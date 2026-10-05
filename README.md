# NHL 27 PS5 Frostbite Extractor

Read-only tools for inspecting and extracting NHL 27 PS5 Frostbite TOC/CAS archives outside the game directory.

## Components

- `nhl27_ps5_extractor.py` parses base and Patch PS5 TOCs, bundle maps, CAS entries, Frostbite bundle manifests and CAS blocks (none/zlib/LZ4/Zstd/Oodle via the FMT native libraries).
- `Nhl27LayoutProbe` uses the local Frosty SDK to inspect the layout manifest and Frostbite bundle assets.

## Usage

```powershell
# Player database (8906 players) as players.json / players.csv,
# plus ai_skills.json / ai_skills.csv for skater and goalie ratings
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --export-player-db .\extracted\player_db

# Find bundles by name, then extract every EBX/RES/chunk asset of one bundle
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/contentsb.toc --find-bundle nyr/adidas
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/contentsb.toc --extract-bundle 3 --extract-dir .\extracted\nyr_home

# Loose chunks of a TOC (JSON configs, textures, audio) and a header survey
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/globals.toc --extract-all-chunks .\extracted\globals_chunks
python nhl27_ps5_extractor.py "F:\NHL 27\PPSA34063-app0" --toc Data/Ps5/globals.toc --chunk-survey .\globals_chunk_survey.json
```

Patch TOCs (`Patch/Ps5/*.toc`) are supported; each entry's descriptor selects whether its payload lives in the `Patch` or `Data` CAS layer.

All generated reports, raw asset exports, decoded payloads, models, and build output are excluded from version control. Tools must not write to the game installation directory.