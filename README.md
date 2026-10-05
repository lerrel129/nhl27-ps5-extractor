# NHL 27 PS5 Frostbite Extractor

Read-only tools for inspecting NHL 27 PS5 Frostbite TOC/CAS archives outside the game directory.

## Components

- `nhl27_ps5_extractor.py` parses PS5 TOCs, bundle maps, CAS entries, and uncompressed CAS blocks.
- `Nhl27LayoutProbe` uses the local Frosty SDK to inspect the layout manifest and Frostbite bundle assets.

All generated reports, raw asset exports, decoded payloads, models, and build output are excluded from version control. Tools must not write to the game installation directory.