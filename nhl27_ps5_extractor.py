from __future__ import annotations

import argparse
import csv
import ctypes
import json
import re
import struct
import subprocess
import tempfile
import uuid
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path


PS5_TOC_MAGIC = 0x3C
PS5_PATCH_TOC_MAGIC = 0x00D1CE01
PS5_PATCH_TOC_BASE = 0x22C
HEADER_WORDS = 24
CONTENTLAUNCH_CATALOG_BASE = 0x1B4
PS5_INSTALL_PACKAGE_COUNT = 8
FROSTBITE_BUNDLE_MAGIC = 0x9D798ED6
DEFAULT_FROSTY_DIR = Path(r"G:\NHL mod\mod\FMT")
METADATA_FIELDS = (
    "magic",
    "bundle_offset",
    "bundle_count",
    "chunk_flag_offset",
    "chunk_guid_offset",
    "chunk_count",
    "chunk_entry_offset",
    "unknown_table_offset",
    "name_offset",
    "data_offset",
    "unknown_count",
    "flags",
    "compressed_string_count",
    "compressed_string_size",
    "compressed_string_offset",
)


@dataclass(frozen=True)
class TocHeader:
    path: str
    size: int
    magic: int
    base_offset: int
    words: list[int]
    metadata: dict[str, int]
    tables: dict[str, int]


def package_index_from_catalog(catalog_index: int) -> int:
    if not 0x1B0 <= catalog_index <= 0x1BF:
        raise ValueError(f"Unsupported PS5 catalog index: 0x{catalog_index:04X}.")
    return (catalog_index - CONTENTLAUNCH_CATALOG_BASE) % PS5_INSTALL_PACKAGE_COUNT


def layer_from_descriptor_word(word: int) -> str:
    # Bit 16 of the descriptor's leading word selects the Patch CAS layer over the base Data layer.
    return "Patch" if (word >> 16) & 1 else "Data"


def read_toc_header(toc_path: Path, game_root: Path) -> TocHeader:
    toc_size = toc_path.stat().st_size
    with toc_path.open("rb") as toc_file:
        signature = toc_file.read(4)
        if len(signature) != 4:
            raise ValueError("TOC is shorter than the PS5 header probe.")
        # Patch TOCs carry a Frosty signature block before the regular PS5 header.
        base_offset = PS5_PATCH_TOC_BASE if struct.unpack(">I", signature)[0] == PS5_PATCH_TOC_MAGIC else 0
        toc_file.seek(base_offset)
        raw_header = toc_file.read(HEADER_WORDS * 4)

    if len(raw_header) != HEADER_WORDS * 4:
        raise ValueError("TOC is shorter than the PS5 header probe.")

    words = list(struct.unpack(f">{HEADER_WORDS}I", raw_header))
    if words[0] != PS5_TOC_MAGIC:
        raise ValueError(f"Unsupported TOC magic 0x{words[0]:08X}.")

    metadata = dict(zip(METADATA_FIELDS, words))
    chunk_count = metadata["chunk_count"]
    chunk_entry_offset = metadata["chunk_entry_offset"]
    name_offset = metadata["name_offset"]
    if chunk_count == 0 or name_offset <= chunk_entry_offset:
        raise ValueError("TOC has no readable PS5 chunk table.")

    chunk_entry_bytes = name_offset - chunk_entry_offset
    if chunk_entry_bytes % chunk_count != 0:
        raise ValueError("PS5 chunk table does not divide evenly by its chunk count.")

    tables = {
        "chunk_flags_end": metadata["chunk_flag_offset"] + chunk_count * 4,
        "chunk_guids_end": metadata["chunk_guid_offset"] + chunk_count * 20,
        "chunk_entry_size": chunk_entry_bytes // chunk_count,
        "chunk_entries_end": name_offset,
    }
    if any(base_offset + value > toc_size for key, value in tables.items() if key.endswith("_end")):
        raise ValueError("PS5 chunk table extends beyond the TOC file.")

    return TocHeader(
        path=toc_path.relative_to(game_root).as_posix(),
        size=toc_size,
        magic=words[0],
        base_offset=base_offset,
        words=words,
        metadata=metadata,
        tables=tables,
    )


def read_layout_manifest_strings(layout_path: Path) -> list[str]:
    if not layout_path.is_file():
        return []

    content = layout_path.read_bytes()
    strings = [
        match.decode("ascii")
        for match in re.findall(rb"[ -~]{6,}", content)
        if b"Ps5/" in match or b"superbundlelayout/" in match
    ]
    return list(dict.fromkeys(strings))


def read_chunk_descriptors(toc_path: Path, game_root: Path) -> dict[str, object]:
    header = read_toc_header(toc_path, game_root)
    metadata = header.metadata
    chunk_count = metadata["chunk_count"]
    base = header.base_offset

    descriptors: list[dict[str, int | str]] = []
    with toc_path.open("rb") as toc_file:
        for index in range(chunk_count):
            toc_file.seek(base + metadata["chunk_flag_offset"] + index * 4)
            flag = struct.unpack(">I", toc_file.read(4))[0]

            toc_file.seek(base + metadata["chunk_guid_offset"] + index * 20)
            guid = toc_file.read(16).hex()
            guid_index = struct.unpack(">I", toc_file.read(4))[0]

            toc_file.seek(base + metadata["chunk_entry_offset"] + index * header.tables["chunk_entry_size"])
            descriptor = struct.unpack(">4I", toc_file.read(16))
            descriptors.append(
                {
                    "index": index,
                    "flag": flag,
                    "guid": guid,
                    "guid_index": guid_index,
                    "unknown_word": descriptor[0],
                    "layer": layer_from_descriptor_word(descriptor[0]),
                    "catalog_and_cas": descriptor[1],
                    "cas_offset": descriptor[2],
                    "size": descriptor[3],
                }
            )

    return {
        "toc": header.path,
        "base_offset": base,
        "chunk_count": chunk_count,
        "chunk_entry_size": header.tables["chunk_entry_size"],
        "chunks": descriptors,
    }


def decode_compressed_string(bit_index: int, table: list[int], data: list[int]) -> str | None:
    try:
        characters: list[str] = []
        while True:
            node = len(table) // 2 - 1
            while node >= 0:
                bit = (data[bit_index // 32] >> (bit_index % 32)) & 1
                node = table[node * 2 + bit]
                bit_index += 1
            character = chr(-1 - node)
            if character == "\0":
                return "".join(characters)
            characters.append(character)
    except (IndexError, ValueError):
        return None


def read_bundle_index(toc_path: Path, game_root: Path) -> dict[str, object]:
    header = read_toc_header(toc_path, game_root)
    metadata = header.metadata
    bundle_count = metadata["bundle_count"]
    base = header.base_offset

    with toc_path.open("rb") as toc_file:
        toc_file.seek(base + metadata["name_offset"])
        string_data = list(
            struct.unpack(f">{metadata['compressed_string_count']}I", toc_file.read(metadata["compressed_string_count"] * 4))
        )
        toc_file.seek(base + metadata["compressed_string_offset"])
        string_table = list(
            struct.unpack(f">{metadata['compressed_string_size']}i", toc_file.read(metadata["compressed_string_size"] * 4))
        )

        bundles: list[dict[str, int | str | None]] = []
        toc_file.seek(base + metadata["bundle_offset"])
        for index in range(bundle_count):
            name_bit_offset, size, offset = struct.unpack(">IIQ", toc_file.read(16))
            bundles.append(
                {
                    "index": index,
                    "name": decode_compressed_string(name_bit_offset, string_table, string_data),
                    "size": size,
                    "offset": offset,
                }
            )

    return {
        "toc": header.path,
        "base_offset": base,
        "bundle_count": bundle_count,
        "bundles": bundles,
    }


def read_bundle_entries_from_index(
    toc_path: Path, bundle_report: dict[str, object], bundle_index: int
) -> dict[str, object]:
    bundles = bundle_report["bundles"]
    if bundle_index < 0 or bundle_index >= len(bundles):
        raise ValueError(f"Bundle index must be between 0 and {len(bundles) - 1}.")

    bundle = bundles[bundle_index]
    bundle_offset = int(bundle_report["base_offset"]) + int(bundle["offset"])
    with toc_path.open("rb") as toc_file:
        toc_file.seek(bundle_offset)
        header = toc_file.read(36)
        if len(header) != 36:
            raise ValueError("PS5 bundle header is incomplete.")
        words = struct.unpack(">9I", header)
        flag_offset = words[2]
        entry_count = words[3]
        entry_offset = words[4]
        if entry_offset < len(header) or flag_offset < entry_offset:
            raise ValueError("PS5 bundle header has invalid entry table offsets.")

        toc_file.seek(bundle_offset + flag_offset)
        flags = toc_file.read(entry_count)
        if len(flags) != entry_count:
            raise ValueError("PS5 bundle flag table is incomplete.")

        expected_entry_bytes = entry_count * 8 + sum(8 for flag in flags if flag)
        if entry_offset + expected_entry_bytes != flag_offset:
            raise ValueError("PS5 bundle entry table length does not match its flag table.")

        toc_file.seek(bundle_offset + entry_offset)
        entries: list[dict[str, int]] = []
        catalog_and_cas: int | None = None
        layer = "Data"
        for index, flag in enumerate(flags):
            unknown_word = None
            if flag:
                unknown_word, catalog_and_cas = struct.unpack(">II", toc_file.read(8))
                layer = layer_from_descriptor_word(unknown_word)
            if catalog_and_cas is None:
                raise ValueError("PS5 bundle entry has no inherited catalog descriptor.")
            cas_offset, size = struct.unpack(">II", toc_file.read(8))
            entries.append(
                {
                    "index": index,
                    "flag": flag,
                    "unknown_word": unknown_word,
                    "layer": layer,
                    "catalog_and_cas": catalog_and_cas,
                    "cas_offset": cas_offset,
                    "size": size,
                }
            )

    return {
        "toc": bundle_report["toc"],
        "bundle": bundle,
        "entry_count": entry_count,
        "entries": entries,
    }


def read_bundle_entries(toc_path: Path, game_root: Path, bundle_index: int) -> dict[str, object]:
    return read_bundle_entries_from_index(toc_path, read_bundle_index(toc_path, game_root), bundle_index)


def resolve_cas_path(game_root: Path, layer: str, catalog_and_cas: int) -> tuple[Path, int, int]:
    catalog_index = catalog_and_cas >> 16
    cas_index = catalog_and_cas & 0xFFFF
    package_index = package_index_from_catalog(catalog_index)
    cas_path = (
        game_root
        / layer
        / "Ps5"
        / "superbundlelayout"
        / f"nhl_installpackage_{package_index:02d}"
        / f"cas_{cas_index:02d}.cas"
    )
    if not cas_path.is_file():
        raise FileNotFoundError(f"Mapped CAS file was not found: {cas_path}")
    return cas_path, catalog_index, package_index


def read_cas_entry(cas_path: Path, offset: int, size: int) -> bytes:
    if offset + size > cas_path.stat().st_size:
        raise ValueError(f"Entry at {offset}+{size} exceeds the bounds of {cas_path.name}.")
    with cas_path.open("rb") as cas_file:
        cas_file.seek(offset)
        data = cas_file.read(size)
    if len(data) != size:
        raise ValueError(f"Could not read the complete entry from {cas_path.name}.")
    return data


def write_entry_with_sidecar(output_path: Path, data: bytes, metadata: dict[str, object]) -> None:
    output_path.write_bytes(data)
    output_path.with_suffix(output_path.suffix + ".json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )


def extract_toc_chunk(game_root: Path, toc_relative_path: str, chunk_index: int, output_path: Path) -> None:
    chunk_report = read_chunk_descriptors(game_root / toc_relative_path, game_root)
    chunks = chunk_report["chunks"]
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise ValueError(f"Chunk index must be between 0 and {len(chunks) - 1}.")

    chunk = chunks[chunk_index]
    cas_path, catalog_index, package_index = resolve_cas_path(
        game_root, str(chunk["layer"]), int(chunk["catalog_and_cas"])
    )
    raw_chunk = read_cas_entry(cas_path, int(chunk["cas_offset"]), int(chunk["size"]))
    write_entry_with_sidecar(
        output_path,
        raw_chunk,
        {
            "toc": chunk_report["toc"],
            "chunk": chunk,
            "catalog_index": catalog_index,
            "package_index": package_index,
            "cas_path": cas_path.relative_to(game_root).as_posix(),
        },
    )


def extract_all_toc_chunks(
    game_root: Path, toc_relative_path: str, output_dir: Path, frosty_dir: Path
) -> dict[str, object]:
    chunk_report = read_chunk_descriptors(game_root / toc_relative_path, game_root)
    codecs = CasCodecs(frosty_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    for chunk in chunk_report["chunks"]:
        guid = str(uuid.UUID(bytes_le=bytes.fromhex(str(chunk["guid"]))))
        record: dict[str, object] = {"index": chunk["index"], "id": guid}
        try:
            cas_path, _, _ = resolve_cas_path(game_root, str(chunk["layer"]), int(chunk["catalog_and_cas"]))
            payload = decode_cas_blocks(read_cas_entry(cas_path, int(chunk["cas_offset"]), int(chunk["size"])), codecs)
            (output_dir / f"{guid}.chunk").write_bytes(payload)
            record["size"] = len(payload)
        except (ValueError, FileNotFoundError, OSError) as error:
            record["error"] = str(error)
        results.append(record)
    report = {
        "toc": chunk_report["toc"],
        "extracted": sum(1 for record in results if "size" in record),
        "failed": sum(1 for record in results if "error" in record),
        "chunks": results,
    }
    (output_dir / "chunks_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def flatten_json(value: object, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            flat.update(flatten_json(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            flat.update(flatten_json(child, f"{prefix}{index}."))
    else:
        flat[prefix.rstrip(".")] = value
    return flat


def export_player_database(game_root: Path, output_dir: Path, frosty_dir: Path) -> dict[str, object]:
    """Decodes the loose globals chunks and exports the per-player JSON documents as one database."""
    layers = [("Data", "Data/Ps5/globals.toc"), ("Patch", "Patch/Ps5/globals.toc")]
    codecs = CasCodecs(frosty_dir)
    players: dict[str, dict[str, object]] = {}
    teams: dict[str, dict[str, object]] = {}
    documents: dict[str, list[dict[str, object]]] = {}
    for layer, toc_relative_path in layers:
        toc_path = game_root / toc_relative_path
        if not toc_path.is_file():
            continue
        for chunk in read_chunk_descriptors(toc_path, game_root)["chunks"]:
            try:
                cas_path, _, _ = resolve_cas_path(game_root, str(chunk["layer"]), int(chunk["catalog_and_cas"]))
                raw = read_cas_entry(cas_path, int(chunk["cas_offset"]), int(chunk["size"]))
                # Peek past the 8-byte CAS block header before paying for a full decode.
                if not raw[8:12].lstrip().startswith((b"{", b"[")):
                    continue
                payload = decode_cas_blocks(raw, codecs)
                document = json.loads(payload)
            except (ValueError, FileNotFoundError, json.JSONDecodeError):
                continue
            guid = str(uuid.UUID(bytes_le=bytes.fromhex(str(chunk["guid"]))))
            if isinstance(document, dict) and "Attribute" in document and "Appearance" in document:
                # Patch chunks override base chunks that share the same GUID.
                players[guid] = {"chunk_id": guid, "layer": layer, **document}
            elif (
                isinstance(document, dict)
                and isinstance(document.get("GeneralInfo"), dict)
                and "TeamName" in document["GeneralInfo"]
            ):
                teams[guid] = {"chunk_id": guid, "layer": layer, **document}
            else:
                kind = next(iter(document)) if isinstance(document, dict) and document else "list"
                documents.setdefault(str(kind), []).append({"chunk_id": guid, "layer": layer, "size": len(payload)})

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "players.json").write_text(json.dumps(list(players.values()), indent=1), encoding="utf-8")

    rows = [flatten_json({key: value for key, value in player.items() if key in ("chunk_id", "layer", "Attribute", "Ai")}) for player in players.values()]
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with (output_dir / "players.csv").open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    ai_rows: list[dict[str, object]] = []
    for player in players.values():
        attribute = player.get("Attribute", {})
        ai = player.get("Ai", {})
        is_goalie = bool(ai.get("IsGoalie"))
        skills = ai.get("GoalieAi" if is_goalie else "SkaterAi", {})
        active_xfactors = [
            ability
            for ability in ai.get("XFactorAbilities", [])
            if ability.get("Id", -1) >= 0 and ability.get("Status", 0) > 0
        ]
        ai_rows.append(
            {
                "chunk_id": player["chunk_id"],
                "player_id": attribute.get("Id"),
                "first_name": attribute.get("FirstName"),
                "last_name": attribute.get("LastName"),
                "position": attribute.get("PosType"),
                "jersey_number": attribute.get("JerseyNum"),
                "role": "Goalie" if is_goalie else "Skater",
                "overall": skills.get("Overall"),
                "potential": skills.get("Potential"),
                "growth_tier": skills.get("GrowthTier"),
                "xfactors": ";".join(
                    f"{ability['Id']}:{ability.get('Tier', 0)}:{ability.get('Status', 0)}"
                    for ability in active_xfactors
                ),
                **{f"ai_{key}": value for key, value in skills.items()},
            }
        )
    ai_columns = sorted({key for row in ai_rows for key in row})
    with (output_dir / "ai_skills.csv").open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=ai_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ai_rows)
    (output_dir / "ai_skills.json").write_text(json.dumps(ai_rows, indent=1), encoding="utf-8")

    team_rows: list[dict[str, object]] = []
    team_stats_rows: list[dict[str, object]] = []
    line_rows: list[dict[str, object]] = []
    for team in teams.values():
        info = team["GeneralInfo"]
        team_identity = {
            "chunk_id": team["chunk_id"],
            "layer": team["layer"],
            "team_id": info.get("Id"),
            "stock_team_id": info.get("StockTeamId"),
            "name": info.get("TeamName"),
            "full_name": info.get("FullName"),
            "city": info.get("CityName"),
            "abbreviation": info.get("AbbrName"),
            "league": info.get("League"),
            "league_group": info.get("LeagueGroup"),
            "conference_group": info.get("ConferenceGroup"),
            "division_group": info.get("DivisionGroup"),
        }
        team_rows.append(team_identity)
        team_stats_rows.append(
            {
                **team_identity,
                **{f"stats_{key}": value for key, value in team.get("Stats", {}).items()},
                **{f"rank_{key}": value for key, value in team.get("Rank", {}).items()},
                **{f"streak_{key}": value for key, value in team.get("Streak", {}).items()},
            }
        )
        for line_set in ("DefaultLines", "CurrentLines"):
            for unit, line_data in team.get(line_set, {}).items():
                named_lines = line_data.items() if isinstance(line_data, dict) else [("Default", line_data)]
                for line_name, roster_indices in named_lines:
                    for slot, roster_index in enumerate(roster_indices):
                        line_rows.append(
                            {
                                **team_identity,
                                "line_set": line_set,
                                "unit": unit,
                                "line": line_name,
                                "slot": slot + 1,
                                "roster_index": roster_index,
                            }
                        )

    def write_csv(filename: str, values: list[dict[str, object]]) -> None:
        fieldnames = sorted({key for value in values for key in value})
        with (output_dir / filename).open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(values)

    write_csv("teams.csv", team_rows)
    write_csv("team_stats.csv", team_stats_rows)
    write_csv("team_lines.csv", line_rows)
    (output_dir / "teams.json").write_text(json.dumps(list(teams.values()), indent=1), encoding="utf-8")

    summary = {
        "player_count": len(players),
        "team_count": len(teams),
        "team_line_count": len(line_rows),
        "ai_skill_columns": ai_columns,
        "other_documents": {kind: len(items) for kind, items in documents.items()},
        "columns": columns,
    }
    (output_dir / "summary.json").write_text(json.dumps({**summary, "documents": documents}, indent=2), encoding="utf-8")
    return summary


def survey_toc_chunks(game_root: Path, toc_relative_path: str, probe_bytes: int = 16) -> dict[str, object]:
    """Classifies loose TOC chunks by the first decoded bytes to spot databases, images and audio."""
    chunk_report = read_chunk_descriptors(game_root / toc_relative_path, game_root)
    codecs = CasCodecs(DEFAULT_FROSTY_DIR)
    signatures: dict[str, dict[str, object]] = {}
    for chunk in chunk_report["chunks"]:
        try:
            cas_path, _, _ = resolve_cas_path(game_root, str(chunk["layer"]), int(chunk["catalog_and_cas"]))
            # Only the first CAS block is needed to read the leading bytes.
            head = read_cas_entry(cas_path, int(chunk["cas_offset"]), min(int(chunk["size"]), 0x10008))
            decoded = decode_cas_blocks(head[: 8 + (struct.unpack_from(">H", head, 6)[0] + ((head[5] & 0x0F) << 16))], codecs)
        except (ValueError, FileNotFoundError, IndexError, struct.error):
            continue
        key = decoded[:probe_bytes].hex()
        bucket = signatures.setdefault(key, {"count": 0, "total_size": 0, "ascii": decoded[:probe_bytes].decode("ascii", errors="replace"), "examples": []})
        bucket["count"] = int(bucket["count"]) + 1
        bucket["total_size"] = int(bucket["total_size"]) + int(chunk["size"])
        if len(bucket["examples"]) < 3:
            bucket["examples"].append({"index": chunk["index"], "size": chunk["size"]})
    ordered = sorted(signatures.items(), key=lambda item: -int(item[1]["count"]))
    return {"toc": chunk_report["toc"], "chunk_count": chunk_report["chunk_count"], "signatures": dict(ordered)}


def extract_bundle_entry(
    game_root: Path,
    toc_relative_path: str,
    bundle_index: int,
    entry_index: int,
    output_path: Path,
) -> None:
    entries_report = read_bundle_entries(game_root / toc_relative_path, game_root, bundle_index)
    entries = entries_report["entries"]
    if entry_index < 0 or entry_index >= len(entries):
        raise ValueError(f"Entry index must be between 0 and {len(entries) - 1}.")

    entry = entries[entry_index]
    cas_path, catalog_index, package_index = resolve_cas_path(
        game_root, str(entry["layer"]), int(entry["catalog_and_cas"])
    )
    raw_asset = read_cas_entry(cas_path, int(entry["cas_offset"]), int(entry["size"]))
    write_entry_with_sidecar(
        output_path,
        raw_asset,
        {
            "toc": entries_report["toc"],
            "bundle": entries_report["bundle"],
            "entry": entry,
            "catalog_index": catalog_index,
            "package_index": package_index,
            "cas_path": cas_path.relative_to(game_root).as_posix(),
        },
    )


def read_c_string(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        raise ValueError("Bundle string table is not null-terminated.")
    return data[offset:end].decode("utf-8", errors="replace")


def parse_frostbite_bundle(data: bytes) -> dict[str, object]:
    if len(data) < 36:
        raise ValueError("Frostbite bundle is shorter than its header.")
    magic = struct.unpack_from("<I", data, 4)[0]
    if magic != FROSTBITE_BUNDLE_MAGIC:
        raise ValueError(f"Unsupported Frostbite bundle magic 0x{magic:08X}.")

    total_count, ebx_count, res_count, chunk_count, strings_offset, meta_offset, meta_size = struct.unpack_from(
        "<7I", data, 8
    )
    if total_count != ebx_count + res_count + chunk_count:
        raise ValueError("Frostbite bundle asset counts are inconsistent.")
    # All bundle offsets are relative to the end of the leading 4-byte size field.
    base = 4
    strings_base = base + strings_offset
    if strings_base > len(data) or base + meta_offset + meta_size > len(data):
        raise ValueError("Frostbite bundle tables extend beyond the bundle data.")

    position = 36 + total_count * 20
    sha1s = [data[36 + index * 20 : 36 + (index + 1) * 20].hex() for index in range(total_count)]

    ebx: list[dict[str, object]] = []
    for index in range(ebx_count):
        name_offset, original_size = struct.unpack_from("<II", data, position)
        position += 8
        ebx.append({"name": read_c_string(data, strings_base + name_offset), "original_size": original_size, "sha1": sha1s[index]})

    res: list[dict[str, object]] = []
    for index in range(res_count):
        name_offset, original_size = struct.unpack_from("<II", data, position)
        position += 8
        res.append(
            {
                "name": read_c_string(data, strings_base + name_offset),
                "original_size": original_size,
                "sha1": sha1s[ebx_count + index],
            }
        )
    for entry in res:
        entry["res_type"] = struct.unpack_from("<I", data, position)[0]
        position += 4
    for entry in res:
        entry["res_meta"] = data[position : position + 16].hex()
        position += 16
    for entry in res:
        entry["res_rid"] = struct.unpack_from("<Q", data, position)[0]
        position += 8

    chunks: list[dict[str, object]] = []
    for index in range(chunk_count):
        guid = uuid.UUID(bytes_le=data[position : position + 16])
        logical_offset, logical_size = struct.unpack_from("<II", data, position + 16)
        position += 24
        chunks.append(
            {
                "id": str(guid),
                "logical_offset": logical_offset,
                "logical_size": logical_size,
                "sha1": sha1s[ebx_count + res_count + index],
            }
        )

    return {
        "total_count": total_count,
        "ebx_count": ebx_count,
        "res_count": res_count,
        "chunk_count": chunk_count,
        "ebx": ebx,
        "res": res,
        "chunks": chunks,
    }


def safe_asset_path(root: Path, name: str, suffix: str) -> Path:
    cleaned = re.sub(r"[^A-Za-z0-9_./-]", "_", name).strip("/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".", "..")]
    if not parts:
        raise ValueError(f"Asset name {name!r} does not yield a usable path.")
    parts[-1] += suffix
    return root.joinpath(*parts)


def make_dds_dx10(width: int, height: int, dxgi_format: int, slices: list[bytes]) -> bytes:
    flags = 0x00081007  # CAPS | HEIGHT | WIDTH | PIXELFORMAT | LINEARSIZE
    caps = 0x00001000 | (0x00000008 if len(slices) > 1 else 0)
    header = struct.pack(
        "<31I",
        124,
        flags,
        height,
        width,
        len(slices[0]),
        0,
        1,
        *([0] * 11),
        32,
        4,
        int.from_bytes(b"DX10", "little"),
        0,
        0,
        0,
        0,
        0,
        caps,
        0,
        0,
        0,
        0,
    )
    dx10 = struct.pack("<5I", dxgi_format, 3, 0, len(slices), 0)
    return b"DDS " + header + dx10 + b"".join(slices)


def unswizzle_compressed_mip(width: int, height: int, data: bytes, block_bytes: int) -> bytes:
    """Converts Frosty's PS4-swizzled 8x8 compressed blocks into row-major order."""
    block_width = max(1, (width + 3) // 4)
    block_height = max(1, (height + 3) // 4)
    expected_size = block_width * block_height * block_bytes
    if len(data) != expected_size:
        raise ValueError(f"Compressed mip has {len(data)} bytes; expected {expected_size}.")
    output = bytearray(len(data))
    if block_width % 8 or block_height % 8:
        raise ValueError("PS4-swizzled top mip must contain complete 8x8 block tiles.")

    def morton(index: int) -> int:
        x = y = 0
        x_bit = y_bit = 1
        sx = sy = 8
        while sx > 1 or sy > 1:
            if sx > 1:
                x += x_bit * (index & 1)
                index >>= 1
                x_bit *= 2
                sx >>= 1
            if sy > 1:
                y += y_bit * (index & 1)
                index >>= 1
                y_bit *= 2
                sy >>= 1
        return y * 8 + x

    source_block = 0
    for tile_y in range(block_height // 8):
        for tile_x in range(block_width // 8):
            for index in range(64):
                target = morton(index)
                target_x, target_y = target % 8, target // 8
                x, y = tile_x * 8 + target_x, tile_y * 8 + target_y
                target_block = y * block_width + x
                output[target_block * block_bytes : (target_block + 1) * block_bytes] = data[
                    source_block * block_bytes : (source_block + 1) * block_bytes
                ]
                source_block += 1
    return bytes(output)


def decode_bc1_rgba(width: int, height: int, data: bytes) -> bytes:
    expected_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
    if len(data) != expected_size:
        raise ValueError(f"BC1 top mip has {len(data)} bytes; expected {expected_size}.")
    pixels = bytearray(width * height * 4)
    offset = 0
    for block_y in range(0, height, 4):
        for block_x in range(0, width, 4):
            color0, color1, selectors = struct.unpack_from("<HHI", data, offset)
            offset += 8

            def rgb565(color: int) -> tuple[int, int, int]:
                return (
                    ((color >> 11) & 0x1F) * 255 // 31,
                    ((color >> 5) & 0x3F) * 255 // 63,
                    (color & 0x1F) * 255 // 31,
                )

            first, second = rgb565(color0), rgb565(color1)
            palette = [(*first, 255), (*second, 255)]
            if color0 > color1:
                palette.extend(
                    [
                        tuple((2 * first[index] + second[index]) // 3 for index in range(3)) + (255,),
                        tuple((first[index] + 2 * second[index]) // 3 for index in range(3)) + (255,),
                    ]
                )
            else:
                palette.extend(
                    [
                        tuple((first[index] + second[index]) // 2 for index in range(3)) + (255,),
                        (0, 0, 0, 0),
                    ]
                )
            for pixel_y in range(4):
                for pixel_x in range(4):
                    output_x, output_y = block_x + pixel_x, block_y + pixel_y
                    if output_x >= width or output_y >= height:
                        continue
                    color = palette[(selectors >> (2 * (pixel_y * 4 + pixel_x))) & 3]
                    pixel_offset = (output_y * width + output_x) * 4
                    pixels[pixel_offset : pixel_offset + 4] = bytes(color)
    return bytes(pixels)


def make_png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    def png_chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(
            ">I", len(data)
        ) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\0" + pixels[index : index + width * 4] for index in range(0, len(pixels), width * 4))
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(rows, 9)) + png_chunk(b"IEND", b"")


def convert_frostbite_textures(texture_dir: Path, frosty_dir: Path) -> dict[str, object]:
    """Converts supported Frostbite BC textures, including PS4-swizzled arrays, into DDS and PNG files."""
    res_root = texture_dir / "res"
    chunk_root = texture_dir / "chunks"
    output_root = texture_dir / "converted"
    texture_formats = {
        0x37: ("BC1_SRGB", 72, 8),
        0x3C: ("BC3_UNORM", 77, 16),
        0x3D: ("BC3_SRGB", 78, 16),
        0x3F: ("BC5_UNORM", 83, 16),
    }
    texconv = frosty_dir / "ThirdParty" / "texconv.exe"
    if not texconv.is_file():
        texconv = frosty_dir / "texconv.exe"
    results: list[dict[str, object]] = []
    for res_path in res_root.rglob("*.res"):
        record: dict[str, object] = {"resource": res_path.relative_to(texture_dir).as_posix()}
        try:
            header = res_path.read_bytes()
            if len(header) != 184:
                raise ValueError(f"Expected a 184-byte texture RES header, got {len(header)} bytes.")
            texture_format = struct.unpack_from("<I", header, 12)[0]
            format_info = texture_formats.get(texture_format)
            if format_info is None:
                raise ValueError(f"Unsupported Frostbite texture format enum 0x{texture_format:X}.")
            format_name, dxgi_format, block_bytes = format_info
            width, height = struct.unpack_from("<HH", header, 0x16)
            texture_type = struct.unpack_from("<I", header, 8)[0]
            flags = struct.unpack_from("<H", header, 0x14)[0]
            array_size = struct.unpack_from("<H", header, 0x1A)[0] if texture_type == 3 else 1
            if array_size < 1:
                raise ValueError("Texture array has no slices.")
            chunk_id = str(uuid.UUID(bytes_le=header[0x28:0x38]))
            chunk = (chunk_root / f"{chunk_id}.chunk").read_bytes()
            top_mip_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_bytes
            if len(chunk) < top_mip_size * array_size:
                raise ValueError(
                    f"Chunk has {len(chunk)} bytes; top mip needs {top_mip_size} bytes x {array_size} slices."
                )
            slices: list[bytes] = []
            for slice_index in range(array_size):
                start = slice_index * top_mip_size
                payload = chunk[start : start + top_mip_size]
                if flags == 0x801:
                    payload = unswizzle_compressed_mip(width, height, payload, block_bytes)
                slices.append(payload)
            relative = res_path.relative_to(res_root).with_suffix("")
            dds_path = output_root / "dds" / relative.with_suffix(".dds")
            dds_path.parent.mkdir(parents=True, exist_ok=True)
            dds_path.write_bytes(make_dds_dx10(width, height, dxgi_format, slices))
            png_paths: list[str] = []
            png_root = output_root / "png" / relative
            png_root.parent.mkdir(parents=True, exist_ok=True)
            if texture_format == 0x37:
                for slice_index, payload in enumerate(slices):
                    png_path = png_root.with_name(
                        f"{png_root.name}_slice_{slice_index:03d}.png" if array_size > 1 else f"{png_root.name}.png"
                    )
                    png_path.parent.mkdir(parents=True, exist_ok=True)
                    png_path.write_bytes(make_png_rgba(width, height, decode_bc1_rgba(width, height, payload)))
                    png_paths.append(png_path.relative_to(texture_dir).as_posix())
            else:
                if not texconv.is_file():
                    raise ValueError(f"BC3/BC5 PNG conversion needs texconv.exe under {frosty_dir}.")
                for slice_index, payload in enumerate(slices):
                    png_stem = f"{png_root.name}_slice_{slice_index:03d}" if array_size > 1 else png_root.name
                    png_path = png_root.with_name(f"{png_stem}.png")
                    png_path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="texture_slice_", dir=output_root) as temporary_dir:
                        input_dds = Path(temporary_dir) / f"{png_stem}.dds"
                        input_dds.write_bytes(make_dds_dx10(width, height, dxgi_format, [payload]))
                        conversion = subprocess.run(
                            [str(texconv), "-nologo", "-y", "-ft", "png", "-o", str(png_path.parent), str(input_dds)],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        generated_png = png_path.parent / f"{png_stem}.png"
                        if conversion.returncode != 0 or not generated_png.is_file():
                            raise ValueError(f"texconv could not convert the texture slice: {conversion.stderr.strip()}")
                    png_paths.append(png_path.relative_to(texture_dir).as_posix())
            record.update(
                {
                    "chunk_id": chunk_id,
                    "format": format_name,
                    "width": width,
                    "height": height,
                    "mip_count": 1,
                    "array_size": array_size,
                    "dds": dds_path.relative_to(texture_dir).as_posix(),
                    "png": png_paths,
                }
            )
        except (OSError, ValueError) as error:
            record["error"] = str(error)
        results.append(record)
    report = {
        "source": str(texture_dir),
        "converted": sum(1 for result in results if "dds" in result),
        "failed": sum(1 for result in results if "error" in result),
        "textures": results,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "conversion_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def extract_bundle(
    game_root: Path,
    toc_relative_path: str,
    bundle_index: int,
    output_dir: Path,
    frosty_dir: Path,
) -> dict[str, object]:
    entries_report = read_bundle_entries(game_root / toc_relative_path, game_root, bundle_index)
    entries = entries_report["entries"]
    manifest_entry = entries[0]
    cas_path, _, _ = resolve_cas_path(game_root, str(manifest_entry["layer"]), int(manifest_entry["catalog_and_cas"]))
    manifest = parse_frostbite_bundle(
        read_cas_entry(cas_path, int(manifest_entry["cas_offset"]), int(manifest_entry["size"]))
    )

    assets: list[tuple[str, Path, dict[str, object]]] = []
    for asset in manifest["ebx"]:
        assets.append(("ebx", safe_asset_path(output_dir / "ebx", asset["name"], ".ebx"), asset))
    for asset in manifest["res"]:
        assets.append(("res", safe_asset_path(output_dir / "res", asset["name"], f".{asset['res_type']:08x}.res"), asset))
    for asset in manifest["chunks"]:
        assets.append(("chunk", output_dir / "chunks" / f"{asset['id']}.chunk", asset))

    if len(entries) != 1 + len(assets):
        raise ValueError(
            f"Bundle lists {len(assets)} assets but the TOC map has {len(entries) - 1} payload entries."
        )

    codecs = CasCodecs(frosty_dir)
    results: list[dict[str, object]] = []
    for entry, (kind, target, asset) in zip(entries[1:], assets):
        record: dict[str, object] = {"kind": kind, "name": asset.get("name", asset.get("id")), "output": None}
        try:
            cas_path, _, _ = resolve_cas_path(game_root, str(entry["layer"]), int(entry["catalog_and_cas"]))
            payload = decode_cas_blocks(read_cas_entry(cas_path, int(entry["cas_offset"]), int(entry["size"])), codecs)
            expected = asset.get("original_size", asset.get("logical_size"))
            if expected and len(payload) != expected:
                record["warning"] = f"Decoded {len(payload)} bytes, bundle expects {expected}."
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            record["output"] = target.relative_to(output_dir).as_posix()
            record["size"] = len(payload)
        except (ValueError, FileNotFoundError, OSError) as error:
            record["error"] = str(error)
        results.append(record)

    report = {
        "toc": entries_report["toc"],
        "bundle": entries_report["bundle"],
        "manifest": manifest,
        "extracted": sum(1 for record in results if record["output"]),
        "failed": sum(1 for record in results if "error" in record),
        "assets": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bundle_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def find_bundles(toc_path: Path, game_root: Path, search_terms: list[str]) -> dict[str, object]:
    lowered_terms = [term.lower() for term in search_terms if term]
    if not lowered_terms:
        raise ValueError("At least one non-empty search term is required.")
    bundle_report = read_bundle_index(toc_path, game_root)
    matches = [
        bundle
        for bundle in bundle_report["bundles"]
        if bundle["name"] and any(term in str(bundle["name"]).lower() for term in lowered_terms)
    ]
    return {"toc": bundle_report["toc"], "search_terms": search_terms, "match_count": len(matches), "matches": matches}


def scan_bundle_manifests(
    game_root: Path,
    toc_relative_path: str,
    search_terms: list[str],
    maximum_manifest_size: int = 2 * 1024 * 1024,
) -> dict[str, object]:
    lowered_terms = [term.lower() for term in search_terms if term]
    if not lowered_terms:
        raise ValueError("At least one non-empty search term is required.")

    toc_path = game_root / toc_relative_path
    bundle_report = read_bundle_index(toc_path, game_root)
    matches: list[dict[str, object]] = []
    scanned_count = 0
    skipped: list[dict[str, object]] = []
    for bundle in bundle_report["bundles"]:
        try:
            first_entry = read_bundle_entries_from_index(toc_path, bundle_report, int(bundle["index"]))["entries"][0]
            size = int(first_entry["size"])
            if size > maximum_manifest_size:
                raise ValueError(f"Manifest of {size} bytes exceeds the scan limit.")
            cas_path, _, _ = resolve_cas_path(game_root, str(first_entry["layer"]), int(first_entry["catalog_and_cas"]))
            manifest = parse_frostbite_bundle(read_cas_entry(cas_path, int(first_entry["cas_offset"]), size))
        except (ValueError, FileNotFoundError) as error:
            skipped.append({"bundle": bundle, "reason": str(error)})
            continue

        scanned_count += 1
        for kind in ("ebx", "res"):
            for asset in manifest[kind]:
                name = str(asset["name"])
                matched_terms = [term for term in lowered_terms if term in name.lower()]
                if matched_terms:
                    matches.append(
                        {
                            "bundle_index": bundle["index"],
                            "bundle_name": bundle["name"],
                            "kind": kind,
                            "name": name,
                            "res_type": asset.get("res_type"),
                            "matched_terms": matched_terms,
                        }
                    )

    return {
        "toc": bundle_report["toc"],
        "search_terms": search_terms,
        "maximum_manifest_size": maximum_manifest_size,
        "scanned_bundle_count": scanned_count,
        "skipped_bundle_count": len(skipped),
        "skipped": skipped,
        "matches": matches,
    }


class CasCodecs:
    """Lazily binds the native decompressors shipped with FMT; stdlib handles zlib."""

    def __init__(self, frosty_dir: Path) -> None:
        self.frosty_dir = frosty_dir
        self._zstd: ctypes.CDLL | None = None
        self._lz4: ctypes.CDLL | None = None
        self._oodle: ctypes.CDLL | None = None

    def _load(self, relative: str) -> ctypes.CDLL:
        library_path = self.frosty_dir / relative
        if not library_path.is_file():
            raise ValueError(f"Native decompressor is missing: {library_path}")
        return ctypes.CDLL(str(library_path))

    def zstd(self, payload: bytes, expected_size: int) -> bytes:
        if self._zstd is None:
            self._zstd = self._load(r"ThirdParty\libzstd.1.5.0.dll")
            self._zstd.ZSTD_decompress.restype = ctypes.c_size_t
            self._zstd.ZSTD_decompress.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
            self._zstd.ZSTD_isError.restype = ctypes.c_uint
            self._zstd.ZSTD_isError.argtypes = (ctypes.c_size_t,)
        output = ctypes.create_string_buffer(expected_size)
        produced = self._zstd.ZSTD_decompress(output, expected_size, payload, len(payload))
        if self._zstd.ZSTD_isError(produced):
            raise ValueError("Zstd block failed to decompress (it may require a dictionary).")
        return output.raw[:produced]

    def lz4(self, payload: bytes, expected_size: int) -> bytes:
        if self._lz4 is None:
            self._lz4 = self._load(r"ThirdParty\liblz4.so.1.8.0.dll")
            self._lz4.LZ4_decompress_safe.restype = ctypes.c_int
            self._lz4.LZ4_decompress_safe.argtypes = (ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
        output = ctypes.create_string_buffer(expected_size)
        produced = self._lz4.LZ4_decompress_safe(payload, output, len(payload), expected_size)
        if produced < 0:
            raise ValueError("LZ4 block failed to decompress.")
        return output.raw[:produced]

    def oodle(self, payload: bytes, expected_size: int) -> bytes:
        if self._oodle is None:
            self._oodle = self._load(r"ThirdParty\Compression\oo2core_9_win64.dll")
            self._oodle.OodleLZ_Decompress.restype = ctypes.c_ssize_t
            self._oodle.OodleLZ_Decompress.argtypes = (
                ctypes.c_char_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_ssize_t,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int,
            )
        output = ctypes.create_string_buffer(expected_size)
        produced = self._oodle.OodleLZ_Decompress(
            payload, len(payload), output, expected_size, 1, 0, 0, None, 0, None, None, None, 0, 3
        )
        if produced != expected_size:
            raise ValueError(f"Oodle block produced {produced} of {expected_size} bytes.")
        return output.raw


CAS_COMPRESSION_NONE = 0x00
CAS_COMPRESSION_ZLIB = 0x02
CAS_COMPRESSION_LZ4 = 0x09
CAS_COMPRESSION_ZSTD = 0x0F
CAS_COMPRESSION_OODLE = 0x11


def decode_cas_blocks(raw_chunk: bytes, codecs: CasCodecs | None = None) -> bytes:
    codecs = codecs or CasCodecs(DEFAULT_FROSTY_DIR)
    decoded = bytearray()
    position = 0
    while position < len(raw_chunk):
        if len(raw_chunk) - position < 8:
            raise ValueError("CAS block has an incomplete 8-byte header.")

        decompressed_size = struct.unpack_from(">I", raw_chunk, position)[0] & 0x00FFFFFF
        packed_size_and_type = struct.unpack_from("<H", raw_chunk, position + 4)[0]
        compressed_size_high = (packed_size_and_type >> 8) & 0xFF
        compression_type = packed_size_and_type & 0x7F
        compressed_size = struct.unpack_from(">H", raw_chunk, position + 6)[0]
        if compressed_size_high & 0x0F:
            compressed_size += (compressed_size_high & 0x0F) << 16

        data_start = position + 8
        data_end = data_start + compressed_size
        if data_end > len(raw_chunk):
            raise ValueError("CAS block payload exceeds the raw chunk bounds.")
        payload = raw_chunk[data_start:data_end]

        if compression_type == CAS_COMPRESSION_NONE:
            if compressed_size != decompressed_size:
                raise ValueError("Uncompressed CAS block size does not match its decoded size.")
            block = payload
        elif compression_type == CAS_COMPRESSION_ZLIB:
            block = zlib.decompress(payload)
        elif compression_type == CAS_COMPRESSION_LZ4:
            block = codecs.lz4(payload, decompressed_size)
        elif compression_type == CAS_COMPRESSION_ZSTD:
            block = codecs.zstd(payload, decompressed_size)
        elif compression_type == CAS_COMPRESSION_OODLE:
            block = codecs.oodle(payload, decompressed_size)
        else:
            raise ValueError(
                f"Unsupported Frostbite CAS compression type 0x{compression_type:02X} at offset {position}."
            )
        if len(block) != decompressed_size:
            raise ValueError(f"CAS block decoded to {len(block)} bytes, expected {decompressed_size}.")

        decoded.extend(block)
        position = data_end

    return bytes(decoded)


def inventory_archives(game_root: Path) -> dict[str, object]:
    toc_headers: list[dict[str, object]] = []
    rejected_tocs: list[dict[str, str]] = []

    for toc_path in sorted(game_root.rglob("*.toc")):
        if "__FMTBackup" in toc_path.parts:
            continue
        try:
            toc_headers.append(asdict(read_toc_header(toc_path, game_root)))
        except ValueError as error:
            rejected_tocs.append(
                {
                    "path": toc_path.relative_to(game_root).as_posix(),
                    "reason": str(error),
                }
            )

    cas_archives = [
        {
            "path": cas_path.relative_to(game_root).as_posix(),
            "size": cas_path.stat().st_size,
        }
        for cas_path in sorted(game_root.rglob("*.cas"))
        if "__FMTBackup" not in cas_path.parts
    ]

    return {
        "game_root": str(game_root),
        "supported_toc_count": len(toc_headers),
        "rejected_toc_count": len(rejected_tocs),
        "cas_count": len(cas_archives),
        "layout_manifest_paths": read_layout_manifest_strings(game_root / "Data" / "layout.toc"),
        "toc_headers": toc_headers,
        "rejected_tocs": rejected_tocs,
        "cas_archives": cas_archives,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only inventory for NHL 27 PS5 Frostbite TOC/CAS archives."
    )
    parser.add_argument("game_root", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("nhl27_ps5_archive_map.json"),
        help="JSON report path (default: nhl27_ps5_archive_map.json).",
    )
    parser.add_argument(
        "--chunk-report",
        type=Path,
        help="Write raw PS5 chunk descriptors for the specified --toc path.",
    )
    parser.add_argument(
        "--toc",
        default="Data/Ps5/contentlaunchsb.toc",
        help="TOC path relative to the game root for --chunk-report.",
    )
    parser.add_argument(
        "--bundle-report",
        type=Path,
        help="Write decoded bundle names for the specified --toc path.",
    )
    parser.add_argument(
        "--bundle-entries",
        type=int,
        help="Write parsed asset entries for one bundle from the specified --toc path.",
    )
    parser.add_argument(
        "--bundle-entries-output",
        type=Path,
        help="Output file for --bundle-entries.",
    )
    parser.add_argument(
        "--extract-chunk",
        type=int,
        help="Export one raw loose chunk from --toc using the PS5 catalog and layer mapping.",
    )
    parser.add_argument(
        "--extract-contentlaunch-chunk",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--extract-all-chunks",
        type=Path,
        help="Decode every loose chunk of --toc into this directory, named by chunk GUID.",
    )
    parser.add_argument(
        "--chunk-survey",
        type=Path,
        help="Write a JSON summary of leading bytes for all loose chunks in --toc.",
    )
    parser.add_argument(
        "--export-player-db",
        type=Path,
        help="Export the player database (players.json/players.csv) from the globals chunks into this directory.",
    )
    parser.add_argument(
        "--raw-output",
        type=Path,
        help="Output file for --extract-contentlaunch-chunk.",
    )
    parser.add_argument(
        "--extract-bundle-entry",
        type=int,
        help="Export one raw asset from --bundle-entries using the PS5 catalog mapping.",
    )
    parser.add_argument(
        "--entry-index",
        type=int,
        help="Asset entry index for --extract-bundle-entry.",
    )
    parser.add_argument(
        "--scan-bundle-manifests",
        type=Path,
        help="Write a bounded text search report for the first CAS entry in each named bundle.",
    )
    parser.add_argument(
        "--search-text",
        action="append",
        default=[],
        help="ASCII text to search for with --scan-bundle-manifests; may be repeated.",
    )
    parser.add_argument(
        "--decode-raw",
        type=Path,
        help="Decode Frostbite CAS blocks (none/zlib/lz4/zstd/oodle) from a raw chunk file.",
    )
    parser.add_argument(
        "--decoded-output",
        type=Path,
        help="Output file for --decode-raw.",
    )
    parser.add_argument(
        "--find-bundle",
        action="append",
        default=[],
        help="Case-insensitive substring to match against bundle names in --toc; may be repeated.",
    )
    parser.add_argument(
        "--find-output",
        type=Path,
        help="Output JSON for --find-bundle (default: print matches).",
    )
    parser.add_argument(
        "--extract-bundle",
        type=int,
        help="Extract every EBX/RES/chunk asset of one bundle from --toc into --extract-dir.",
    )
    parser.add_argument(
        "--extract-dir",
        type=Path,
        help="Output directory for --extract-bundle (must be outside the game directory).",
    )
    parser.add_argument(
        "--convert-textures",
        type=Path,
        help="Convert extracted Frostbite BC1 RES/chunk pairs in this directory into DDS and PNG files.",
    )
    parser.add_argument(
        "--frosty-dir",
        type=Path,
        default=DEFAULT_FROSTY_DIR,
        help="FMT directory providing libzstd, liblz4 and oo2core native decompressors.",
    )
    arguments = parser.parse_args()

    game_root = arguments.game_root.resolve()
    if not game_root.is_dir():
        parser.error(f"Game root does not exist: {game_root}")
    if arguments.extract_dir is not None and game_root in arguments.extract_dir.resolve().parents:
        parser.error("--extract-dir must not be inside the game directory.")
    if arguments.convert_textures is not None and game_root in arguments.convert_textures.resolve().parents:
        parser.error("--convert-textures must not be inside the game directory.")

    report = inventory_archives(game_root)
    arguments.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if arguments.chunk_report:
        chunk_report = read_chunk_descriptors(game_root / arguments.toc, game_root)
        arguments.chunk_report.write_text(json.dumps(chunk_report, indent=2), encoding="utf-8")
    if arguments.bundle_report:
        bundle_report = read_bundle_index(game_root / arguments.toc, game_root)
        arguments.bundle_report.write_text(json.dumps(bundle_report, indent=2), encoding="utf-8")
    if arguments.bundle_entries is not None:
        if arguments.bundle_entries_output is None:
            parser.error("--bundle-entries-output is required with --bundle-entries.")
        entries_report = read_bundle_entries(game_root / arguments.toc, game_root, arguments.bundle_entries)
        arguments.bundle_entries_output.write_text(json.dumps(entries_report, indent=2), encoding="utf-8")
    if arguments.extract_contentlaunch_chunk is not None:
        if arguments.raw_output is None:
            parser.error("--raw-output is required with --extract-contentlaunch-chunk.")
        extract_toc_chunk(game_root, "Data/Ps5/contentlaunchsb.toc", arguments.extract_contentlaunch_chunk, arguments.raw_output)
    if arguments.extract_chunk is not None:
        if arguments.raw_output is None:
            parser.error("--raw-output is required with --extract-chunk.")
        extract_toc_chunk(game_root, arguments.toc, arguments.extract_chunk, arguments.raw_output)
    if arguments.extract_all_chunks is not None:
        if game_root in arguments.extract_all_chunks.resolve().parents:
            parser.error("--extract-all-chunks must not be inside the game directory.")
        chunk_result = extract_all_toc_chunks(game_root, arguments.toc, arguments.extract_all_chunks, arguments.frosty_dir)
        print(f"Decoded {chunk_result['extracted']} chunks ({chunk_result['failed']} failed) from {chunk_result['toc']}.")
    if arguments.chunk_survey is not None:
        arguments.chunk_survey.write_text(json.dumps(survey_toc_chunks(game_root, arguments.toc), indent=2), encoding="utf-8")
    if arguments.export_player_db is not None:
        if game_root in arguments.export_player_db.resolve().parents:
            parser.error("--export-player-db must not be inside the game directory.")
        db_summary = export_player_database(game_root, arguments.export_player_db, arguments.frosty_dir)
        print(
            f"Exported {db_summary['player_count']} players, {db_summary['team_count']} team variants, "
            f"and {db_summary['team_line_count']} lineup slots to {arguments.export_player_db}."
        )
    if arguments.extract_bundle_entry is not None:
        if arguments.raw_output is None or arguments.entry_index is None:
            parser.error("--raw-output and --entry-index are required with --extract-bundle-entry.")
        extract_bundle_entry(
            game_root,
            arguments.toc,
            arguments.extract_bundle_entry,
            arguments.entry_index,
            arguments.raw_output,
        )
    if arguments.scan_bundle_manifests:
        scan_report = scan_bundle_manifests(game_root, arguments.toc, arguments.search_text)
        arguments.scan_bundle_manifests.write_text(json.dumps(scan_report, indent=2), encoding="utf-8")
    if arguments.decode_raw:
        if arguments.decoded_output is None:
            parser.error("--decoded-output is required with --decode-raw.")
        arguments.decoded_output.write_bytes(
            decode_cas_blocks(arguments.decode_raw.read_bytes(), CasCodecs(arguments.frosty_dir))
        )
    if arguments.find_bundle:
        found = find_bundles(game_root / arguments.toc, game_root, arguments.find_bundle)
        if arguments.find_output:
            arguments.find_output.write_text(json.dumps(found, indent=2), encoding="utf-8")
        else:
            for bundle in found["matches"]:
                print(f"{bundle['index']:6d}  {bundle['name']}")
        print(f"Matched {found['match_count']} bundles in {found['toc']}.")
    if arguments.extract_bundle is not None:
        if arguments.extract_dir is None:
            parser.error("--extract-dir is required with --extract-bundle.")
        bundle_result = extract_bundle(
            game_root, arguments.toc, arguments.extract_bundle, arguments.extract_dir, arguments.frosty_dir
        )
        print(
            f"Extracted {bundle_result['extracted']} assets "
            f"({bundle_result['failed']} failed) from {bundle_result['bundle']['name']} to {arguments.extract_dir}."
        )
    if arguments.convert_textures is not None:
        texture_result = convert_frostbite_textures(arguments.convert_textures, arguments.frosty_dir)
        print(
            f"Converted {texture_result['converted']} textures to DDS and PNG "
            f"({texture_result['failed']} failed) in {arguments.convert_textures}."
        )
    print(
        "Inventoried "
        f"{report['supported_toc_count']} PS5 TOCs, "
        f"{report['cas_count']} CAS archives, and "
        f"{report['rejected_toc_count']} nonstandard TOCs."
    )


if __name__ == "__main__":
    main()