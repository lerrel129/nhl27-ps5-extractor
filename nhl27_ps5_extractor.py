from __future__ import annotations

import argparse
import json
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path


PS5_TOC_MAGIC = 0x3C
HEADER_WORDS = 24
CONTENTLAUNCH_CATALOG_BASE = 0x1B4
PS5_INSTALL_PACKAGE_COUNT = 8
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
    words: list[int]
    metadata: dict[str, int]
    tables: dict[str, int]


def package_index_from_catalog(catalog_index: int) -> int:
    if not 0x1B0 <= catalog_index <= 0x1BF:
        raise ValueError(f"Unsupported PS5 catalog index: 0x{catalog_index:04X}.")
    return (catalog_index - CONTENTLAUNCH_CATALOG_BASE) % PS5_INSTALL_PACKAGE_COUNT


def read_toc_header(toc_path: Path, game_root: Path) -> TocHeader:
    with toc_path.open("rb") as toc_file:
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
    if any(value > toc_path.stat().st_size for key, value in tables.items() if key.endswith("_end")):
        raise ValueError("PS5 chunk table extends beyond the TOC file.")

    return TocHeader(
        path=toc_path.relative_to(game_root).as_posix(),
        size=toc_path.stat().st_size,
        magic=words[0],
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

    descriptors: list[dict[str, int | str]] = []
    with toc_path.open("rb") as toc_file:
        for index in range(chunk_count):
            toc_file.seek(metadata["chunk_flag_offset"] + index * 4)
            flag = struct.unpack(">I", toc_file.read(4))[0]

            toc_file.seek(metadata["chunk_guid_offset"] + index * 20)
            guid = toc_file.read(16).hex()
            guid_index = struct.unpack(">I", toc_file.read(4))[0]

            toc_file.seek(metadata["chunk_entry_offset"] + index * header.tables["chunk_entry_size"])
            descriptor = struct.unpack(">4I", toc_file.read(16))
            descriptors.append(
                {
                    "index": index,
                    "flag": flag,
                    "guid": guid,
                    "guid_index": guid_index,
                    "unknown_word": descriptor[0],
                    "catalog_and_cas": descriptor[1],
                    "cas_offset": descriptor[2],
                    "size_and_flags": descriptor[3],
                    "size_low_24": descriptor[3] & 0x00FFFFFF,
                }
            )

    return {
        "toc": header.path,
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

    with toc_path.open("rb") as toc_file:
        toc_file.seek(metadata["name_offset"])
        string_data = list(
            struct.unpack(f">{metadata['compressed_string_count']}I", toc_file.read(metadata["compressed_string_count"] * 4))
        )
        toc_file.seek(metadata["compressed_string_offset"])
        string_table = list(
            struct.unpack(f">{metadata['compressed_string_size']}i", toc_file.read(metadata["compressed_string_size"] * 4))
        )

        bundles: list[dict[str, int | str | None]] = []
        toc_file.seek(metadata["bundle_offset"])
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
    bundle_offset = int(bundle["offset"])
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
        for index, flag in enumerate(flags):
            unknown_word = None
            if flag:
                unknown_word, catalog_and_cas = struct.unpack(">II", toc_file.read(8))
            if catalog_and_cas is None:
                raise ValueError("PS5 bundle entry has no inherited catalog descriptor.")
            cas_offset, size_and_flags = struct.unpack(">II", toc_file.read(8))
            entries.append(
                {
                    "index": index,
                    "flag": flag,
                    "unknown_word": unknown_word,
                    "catalog_and_cas": catalog_and_cas,
                    "cas_offset": cas_offset,
                    "size_and_flags": size_and_flags,
                    "size_low_24": size_and_flags & 0x00FFFFFF,
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


def extract_contentlaunch_chunk(game_root: Path, chunk_index: int, output_path: Path) -> None:
    toc_path = game_root / "Data" / "Ps5" / "contentlaunchsb.toc"
    chunk_report = read_chunk_descriptors(toc_path, game_root)
    chunks = chunk_report["chunks"]
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise ValueError(f"Chunk index must be between 0 and {len(chunks) - 1}.")

    chunk = chunks[chunk_index]
    catalog_index = int(chunk["catalog_and_cas"]) >> 16
    cas_index = int(chunk["catalog_and_cas"]) & 0xFFFF
    package_index = package_index_from_catalog(catalog_index)

    cas_path = (
        game_root
        / "Data"
        / "Ps5"
        / "superbundlelayout"
        / f"nhl_installpackage_{package_index:02d}"
        / f"cas_{cas_index:02d}.cas"
    )
    offset = int(chunk["cas_offset"])
    size = int(chunk["size_low_24"])
    if not cas_path.is_file():
        raise FileNotFoundError(f"Mapped CAS file was not found: {cas_path}")
    if offset + size > cas_path.stat().st_size:
        raise ValueError("Chunk descriptor exceeds the mapped CAS file bounds.")

    with cas_path.open("rb") as cas_file:
        cas_file.seek(offset)
        raw_chunk = cas_file.read(size)
    if len(raw_chunk) != size:
        raise ValueError("Could not read the complete chunk from its CAS file.")

    output_path.write_bytes(raw_chunk)
    metadata_path = output_path.with_suffix(output_path.suffix + ".json")
    metadata_path.write_text(
        json.dumps(
            {
                "chunk": chunk,
                "catalog_index": catalog_index,
                "package_index": package_index,
                "cas_path": cas_path.relative_to(game_root).as_posix(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


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
    catalog_index = int(entry["catalog_and_cas"]) >> 16
    cas_index = int(entry["catalog_and_cas"]) & 0xFFFF
    package_index = package_index_from_catalog(catalog_index)

    cas_path = (
        game_root
        / "Data"
        / "Ps5"
        / "superbundlelayout"
        / f"nhl_installpackage_{package_index:02d}"
        / f"cas_{cas_index:02d}.cas"
    )
    offset = int(entry["cas_offset"])
    size = int(entry["size_low_24"])
    if not cas_path.is_file():
        raise FileNotFoundError(f"Mapped CAS file was not found: {cas_path}")
    if offset + size > cas_path.stat().st_size:
        raise ValueError("Bundle entry exceeds the mapped CAS file bounds.")

    with cas_path.open("rb") as cas_file:
        cas_file.seek(offset)
        raw_asset = cas_file.read(size)
    if len(raw_asset) != size:
        raise ValueError("Could not read the complete bundle asset from its CAS file.")

    output_path.write_bytes(raw_asset)
    output_path.with_suffix(output_path.suffix + ".json").write_text(
        json.dumps(
            {
                "toc": entries_report["toc"],
                "bundle": entries_report["bundle"],
                "entry": entry,
                "catalog_index": catalog_index,
                "package_index": package_index,
                "cas_path": cas_path.relative_to(game_root).as_posix(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def scan_bundle_manifests(
    game_root: Path,
    toc_relative_path: str,
    search_terms: list[str],
    maximum_manifest_size: int = 2 * 1024 * 1024,
) -> dict[str, object]:
    normalized_terms = [term.encode("ascii").lower() for term in search_terms]
    if not normalized_terms or any(not term for term in normalized_terms):
        raise ValueError("At least one non-empty ASCII search term is required.")

    bundle_report = read_bundle_index(game_root / toc_relative_path, game_root)
    matches: list[dict[str, object]] = []
    scanned_count = 0
    skipped_count = 0
    for bundle in bundle_report["bundles"]:
        bundle_entries = read_bundle_entries_from_index(
            game_root / toc_relative_path, bundle_report, int(bundle["index"])
        )
        first_entry = bundle_entries["entries"][0]
        size = int(first_entry["size_low_24"])
        if size > maximum_manifest_size:
            skipped_count += 1
            continue

        catalog_index = int(first_entry["catalog_and_cas"]) >> 16
        cas_index = int(first_entry["catalog_and_cas"]) & 0xFFFF
        try:
            package_index = package_index_from_catalog(catalog_index)
        except ValueError:
            skipped_count += 1
            continue
        cas_path = (
            game_root
            / "Data"
            / "Ps5"
            / "superbundlelayout"
            / f"nhl_installpackage_{package_index:02d}"
            / f"cas_{cas_index:02d}.cas"
        )
        offset = int(first_entry["cas_offset"])
        if not cas_path.is_file() or offset + size > cas_path.stat().st_size:
            skipped_count += 1
            continue
        with cas_path.open("rb") as cas_file:
            cas_file.seek(offset)
            manifest = cas_file.read(size)
        scanned_count += 1
        lowered_manifest = manifest.lower()
        matched_terms = [term.decode("ascii") for term in normalized_terms if term in lowered_manifest]
        if matched_terms:
            matches.append(
                {
                    "bundle": bundle,
                    "entry": first_entry,
                    "cas_path": cas_path.relative_to(game_root).as_posix(),
                    "matched_terms": matched_terms,
                }
            )

    return {
        "toc": bundle_report["toc"],
        "search_terms": search_terms,
        "maximum_manifest_size": maximum_manifest_size,
        "scanned_bundle_count": scanned_count,
        "skipped_bundle_count": skipped_count,
        "matches": matches,
    }


def decode_cas_blocks(raw_chunk: bytes) -> bytes:
    decoded = bytearray()
    position = 0
    while position < len(raw_chunk):
        if len(raw_chunk) - position < 8:
            raise ValueError("CAS block has an incomplete 8-byte header.")

        decompressed_size = struct.unpack_from(">I", raw_chunk, position)[0]
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
        if compression_type != 0:
            raise ValueError(
                f"Unsupported Frostbite CAS compression type {compression_type} at offset {position}."
            )
        if compressed_size != decompressed_size:
            raise ValueError("Uncompressed CAS block size does not match its decoded size.")

        decoded.extend(raw_chunk[data_start:data_end])
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
        "--extract-contentlaunch-chunk",
        type=int,
        help="Export one raw contentlaunchsb chunk using the verified PS5 catalog mapping.",
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
        help="Decode uncompressed Frostbite CAS blocks from a raw chunk file.",
    )
    parser.add_argument(
        "--decoded-output",
        type=Path,
        help="Output file for --decode-raw.",
    )
    arguments = parser.parse_args()

    game_root = arguments.game_root.resolve()
    if not game_root.is_dir():
        parser.error(f"Game root does not exist: {game_root}")

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
        extract_contentlaunch_chunk(game_root, arguments.extract_contentlaunch_chunk, arguments.raw_output)
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
        arguments.decoded_output.write_bytes(decode_cas_blocks(arguments.decode_raw.read_bytes()))
    print(
        "Inventoried "
        f"{report['supported_toc_count']} PS5 TOCs, "
        f"{report['cas_count']} CAS archives, and "
        f"{report['rejected_toc_count']} nonstandard TOCs."
    )


if __name__ == "__main__":
    main()