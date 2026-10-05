# NHL 27 PS5 Frostbite Extractor

Read-only tools for inspecting and extracting NHL 27 PS5 Frostbite TOC/CAS archives outside the game directory.

## Components

- `nhl27_ps5_extractor.py` parses base and Patch PS5 TOCs, bundle maps, CAS entries, Frostbite bundle manifests and CAS blocks (none/zlib/LZ4/Zstd/Oodle via the FMT native libraries).
- `Nhl27LayoutProbe` uses the local Frosty SDK to inspect the layout manifest and Frostbite bundle assets.

## Usage

```powershell
# Database export: players.json / players.csv, ai_skills.json / ai_skills.csv,
# teams.json / teams.csv, team_stats.csv, and team_lines.csv
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --export-player-db .\extracted\player_db

# After extracting a texture bundle, convert supported BC1/BC3/BC5 texture RES/chunk pairs to DDS and PNG
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --convert-textures .\extracted\textures\faces\pyotr_kochetkov
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --convert-textures .\extracted\textures\jerseys\nyr_home

# Find bundles by name, then extract every EBX/RES/chunk asset of one bundle
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/contentsb.toc --find-bundle nyr/adidas
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/contentsb.toc --extract-bundle 3 --extract-dir .\extracted\nyr_home

# Loose chunks of a TOC (JSON configs, textures, audio) and a header survey
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/globals.toc --extract-all-chunks .\extracted\globals_chunks
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/globals.toc --chunk-survey .\globals_chunk_survey.json
```

Patch TOCs (`Patch/Ps5/*.toc`) are supported; each entry's descriptor selects whether its payload lives in the `Patch` or `Data` CAS layer.

`team_lines.csv` preserves the game roster index for each formation slot. The base loose JSON data does not include the mapping from that index to a player record. Team statistics are stored in the team documents; season statistics for individual players are not part of the base player documents.

All generated reports, raw asset exports, decoded payloads, models, and build output are excluded from version control. Tools must not write to the game installation directory.

Texture conversion supports BC1 sRGB, BC3 UNORM/sRGB, and BC5 UNORM resources with Frosty's PS4-swizzle flag. DDS files contain the top mip and all array slices; PNG output is written once per slice. Large PS5 character textures that contain a complete 512 KiB streamed fragment are exported with the recovered 1024x512 layout and marked `streamed_mip` in `conversion_report.json`; they are not full-resolution top mips. BC3/BC5 PNG conversion uses `texconv.exe` from the configured FMT directory.