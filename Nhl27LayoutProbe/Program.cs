using System.Buffers.Binary;
using System.Text.Json;
using FMT.FileTools;
using FrostySdk;
using FrostySdk.Frostbite.PluginSharedClasses.Readers;
using FrostySdk.IO;

if (args.Length == 3 && args[0] == "--bundle")
{
    return ExportBundle(args[1], args[2]);
}
if (args.Length >= 5 && args[0] == "--scan-ps5-manifests")
{
    return ScanPs5Manifests(args[1], args[2], args[3], args[4..]);
}

if (args.Length != 2)
{
    Console.Error.WriteLine("Usage: Nhl27LayoutProbe <game-root> <output-json>");
    return 1;
}

var gameRoot = Path.GetFullPath(args[0]);
var outputPath = Path.GetFullPath(args[1]);
var layoutPath = Path.Combine(gameRoot, "Data", "layout.toc");

if (!File.Exists(layoutPath))
{
    Console.Error.WriteLine($"Layout manifest was not found: {layoutPath}");
    return 1;
}

using var layoutStream = File.OpenRead(layoutPath);
using var reader = new DbReader(layoutStream);
var layout = reader.ReadDbObject();
var manifest = layout.GetValue<DbObject>("installManifest");

if (manifest is null)
{
    Console.Error.WriteLine("The layout manifest has no installManifest object.");
    return 1;
}

var catalogs = new List<object>();
foreach (DbObject installChunk in manifest.GetValue<DbObject>("installChunks"))
{
    if (installChunk.GetValue("testDLC", false))
    {
        continue;
    }

    var installBundle = installChunk.HasValue("installBundle")
        ? installChunk.GetValue<string>("installBundle")
        : installChunk.GetValue<string>("name");
    var superBundles = new List<string>();
    if (installChunk.HasValue("superBundles"))
    {
        foreach (string superBundle in installChunk.GetValue<DbObject>("superBundles"))
        {
            superBundles.Add(superBundle);
        }
    }

    var files = new List<object>();
    if (installChunk.HasValue("files"))
    {
        foreach (DbObject file in installChunk.GetValue<DbObject>("files"))
        {
            files.Add(new
            {
                id = file.GetValue("id", 0),
                path = file.GetValue<string>("path").Trim('/'),
            });
        }
    }

    catalogs.Add(new
    {
        id = installChunk.GetValue<Guid>("id"),
        name = installBundle,
        persistentIndex = installChunk.GetValue("PersistentIndex", -1),
        alwaysInstalled = installChunk.GetValue("alwaysInstalled", false),
        superBundles,
        files,
    });
}

var report = new
{
    layout = Path.GetRelativePath(gameRoot, layoutPath),
    catalogCount = catalogs.Count,
    catalogs,
};

File.WriteAllText(outputPath, JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
Console.WriteLine($"Exported {catalogs.Count} install catalogs to {outputPath}");
return 0;

static int ExportBundle(string bundlePath, string outputPath)
{
    using var bundleStream = File.OpenRead(bundlePath);
    using var nativeReader = new NativeReader(bundleStream);
    var bundle = new DbObject();
    var header = new FrostySdk.Frostbite.PluginSharedClasses.Readers.BinaryReader().BinaryRead(
        0, ref bundle, nativeReader, false, false);
    if (header is null)
    {
        Console.Error.WriteLine("The input is not a readable Frostbite bundle.");
        return 1;
    }

    var report = new
    {
        totalCount = header.totalCount,
        ebxCount = header.ebxCount,
        resCount = header.resCount,
        chunkCount = header.chunkCount,
        ebx = GetAssets(bundle, "ebx"),
        res = GetAssets(bundle, "res"),
        chunks = GetAssets(bundle, "chunks"),
    };

    File.WriteAllText(outputPath, JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
    Console.WriteLine($"Exported {header.totalCount} bundle assets to {outputPath}");
    return 0;
}

static List<object> GetAssets(DbObject bundle, string collectionName)
{
    var assets = new List<object>();
    foreach (DbObject asset in bundle.GetValue<DbObject>(collectionName))
    {
        assets.Add(new
        {
            name = asset.HasValue("name") ? asset.GetValue<string>("name") : null,
            originalSize = asset.GetValue("originalSize", 0),
            logicalSize = asset.GetValue("logicalSize", 0),
            resType = asset.GetValue("resType", 0),
            resRid = asset.GetValue("resRid", 0UL),
            resMeta = asset.HasValue("resMeta")
                ? Convert.ToHexString(asset.GetValue<byte[]>("resMeta"))
                : null,
            chunkId = asset.HasValue("id") ? (Guid?)asset.GetValue<Guid>("id") : null,
        });
    }
    return assets;
}

static int ScanPs5Manifests(string gameRoot, string tocRelativePath, string outputPath, string[] searchTerms)
{
    const int maximumManifestSize = 2 * 1024 * 1024;
    var tocPath = Path.Combine(gameRoot, tocRelativePath.Replace('/', Path.DirectorySeparatorChar));
    var matches = new List<object>();
    var parsedCount = 0;
    var skippedCount = 0;

    using var tocStream = File.OpenRead(tocPath);
    tocStream.Position = 4;
    var bundleTableOffset = ReadUInt32BigEndian(tocStream);
    var bundleCount = ReadUInt32BigEndian(tocStream);
    for (var bundleIndex = 0; bundleIndex < bundleCount; bundleIndex++)
    {
        tocStream.Position = bundleTableOffset + bundleIndex * 16L;
        _ = ReadUInt32BigEndian(tocStream);
        _ = ReadUInt32BigEndian(tocStream);
        var bundleOffset = ReadUInt64BigEndian(tocStream);

        tocStream.Position = checked((long)bundleOffset + 8);
        var flagOffset = ReadUInt32BigEndian(tocStream);
        var entryCount = ReadUInt32BigEndian(tocStream);
        var entryOffset = ReadUInt32BigEndian(tocStream);
        if (entryCount == 0)
        {
            skippedCount++;
            continue;
        }

        tocStream.Position = checked((long)bundleOffset + flagOffset);
        var firstEntryFlag = tocStream.ReadByte();
        if (firstEntryFlag <= 0)
        {
            skippedCount++;
            continue;
        }

        tocStream.Position = checked((long)bundleOffset + entryOffset);
        _ = ReadUInt32BigEndian(tocStream);
        var catalogAndCas = ReadUInt32BigEndian(tocStream);
        var casOffset = ReadUInt32BigEndian(tocStream);
        var sizeAndFlags = ReadUInt32BigEndian(tocStream);
        var manifestSize = (int)(sizeAndFlags & 0x00FFFFFF);
        var catalogIndex = catalogAndCas >> 16;
        var casIndex = catalogAndCas & 0xFFFF;
        if (manifestSize > maximumManifestSize || catalogIndex is < 0x1B0 or > 0x1BF)
        {
            skippedCount++;
            continue;
        }

        var packageIndex = ((int)catalogIndex + 4) % 8;
        var casPath = Path.Combine(
            gameRoot,
            "Data",
            "Ps5",
            "superbundlelayout",
            $"nhl_installpackage_{packageIndex:D2}",
            $"cas_{casIndex:D2}.cas");
        if (!File.Exists(casPath) || casOffset + manifestSize > new FileInfo(casPath).Length)
        {
            skippedCount++;
            continue;
        }

        var manifestBytes = new byte[manifestSize];
        using (var casStream = File.OpenRead(casPath))
        {
            casStream.Position = casOffset;
            casStream.ReadExactly(manifestBytes);
        }

        try
        {
            using var manifestStream = new MemoryStream(manifestBytes, writable: false);
            using var nativeReader = new NativeReader(manifestStream);
            var bundle = new DbObject();
            var header = new FrostySdk.Frostbite.PluginSharedClasses.Readers.BinaryReader().BinaryRead(
                0, ref bundle, nativeReader, false, false);
            if (header is null)
            {
                skippedCount++;
                continue;
            }

            parsedCount++;
            FindMatchingAssets(bundle, "ebx", bundleIndex, searchTerms, matches);
            FindMatchingAssets(bundle, "res", bundleIndex, searchTerms, matches);
        }
        catch (Exception)
        {
            skippedCount++;
        }
    }

    var report = new
    {
        toc = tocRelativePath,
        searchTerms,
        maximumManifestSize,
        parsedCount,
        skippedCount,
        matches,
    };
    File.WriteAllText(outputPath, JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
    Console.WriteLine($"Parsed {parsedCount} PS5 bundle manifests; {matches.Count} asset names matched.");
    return 0;
}

static void FindMatchingAssets(
    DbObject bundle, string collectionName, int bundleIndex, string[] searchTerms, List<object> matches)
{
    foreach (DbObject asset in bundle.GetValue<DbObject>(collectionName))
    {
        if (!asset.HasValue("name"))
        {
            continue;
        }
        var name = asset.GetValue<string>("name");
        var matchedTerms = searchTerms.Where(term => name.Contains(term, StringComparison.OrdinalIgnoreCase)).ToList();
        if (matchedTerms.Count > 0)
        {
            matches.Add(new { bundleIndex, collection = collectionName, name, matchedTerms });
        }
    }
}

static uint ReadUInt32BigEndian(Stream stream)
{
    Span<byte> bytes = stackalloc byte[4];
    stream.ReadExactly(bytes);
    return BinaryPrimitives.ReadUInt32BigEndian(bytes);
}

static ulong ReadUInt64BigEndian(Stream stream)
{
    Span<byte> bytes = stackalloc byte[8];
    stream.ReadExactly(bytes);
    return BinaryPrimitives.ReadUInt64BigEndian(bytes);
}