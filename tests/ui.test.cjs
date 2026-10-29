const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const { join } = require("node:path");

function appFunctions(startName, endName, context, exported) {
    const app = readFileSync(
        join(__dirname, "../synthocr/static/app.js"),
        "utf8",
    );
    const start = app.indexOf(startName);
    const end = app.indexOf(endName, start);
    assert.ok(start >= 0 && end > start);
    return runInNewContext(`${app.slice(start, end)}\n(${exported});`, context);
}

test("pattern attributes compile with the browser's unicodeSets flag", () => {
    // Browsers ignore an invalid pattern, silently dropping client validation.
    const html = readFileSync(
        join(__dirname, "../synthocr/static/index.html"),
        "utf8",
    );
    const patterns = [...html.matchAll(/\spattern="([^"]*)"/g)].map(
        (match) => match[1],
    );
    assert.ok(patterns.length > 0);
    for (const pattern of patterns) {
        const expression = new RegExp(`^(?:${pattern})$`, "v");
        assert.ok(expression.test("kurdish-dataset_2"), pattern);
        assert.ok(!expression.test("-leading"), pattern);
    }
});

function settingsFor(style, custom = null, shares = {}) {
    const values = {
        "dataset-name": "training",
        samples: "100",
        "text-size": "64",
    };
    const presets = {
        balanced: {
            settings: {
                augmentation: 0.75,
                background_presets: ["office-scan", "aged-paper"],
                rotation: 2,
                blur: 0.55,
            },
        },
    };
    return appFunctions(
        "// A style is a server preset",
        "function randomSeed()",
        {
            state: {
                info: { image_presets: presets },
                upload: null,
                style,
                custom,
                languageShares: shares,
            },
            $: (id) => ({ value: values[id] }),
            selectedSource: () => "corpus",
            languageParameter: () => "both",
            chosenFontIds: () => ["abc:0"],
            datasetLanguages: () =>
                Object.keys(shares).length
                    ? Object.keys(shares)
                    : ["ckb", "kmr", "ar"],
        },
        "currentSettings",
    )();
}

test("generation puts every image in one set and sends the chosen style", () => {
    const result = settingsFor("balanced");
    assert.equal(result.train_ratio, 1);
    assert.equal(result.validation_ratio, 0);
    assert.equal(result.augmentation, 0.75);
    assert.deepEqual(
        [...result.background_presets],
        ["office-scan", "aged-paper"],
    );
    assert.equal(result.name, "training");
    assert.equal(result.samples, 100);
    assert.deepEqual([...result.languages], ["ckb", "kmr", "ar"]);
    assert.equal(result.font_size, 64);
    assert.deepEqual([...result.fonts], ["abc:0"]);
    for (const removed of ["workers", "format", "seed", "text"])
        assert.ok(!(removed in result), removed);
    assert.ok(!("output_location" in result) && !("output_folder" in result));
});

test("an unknown style falls back to the server defaults instead of guessing", () => {
    const result = settingsFor("missing");
    assert.ok(!("augmentation" in result));
    assert.ok(!("background_presets" in result));
});

test("customizing overrides the effect share, backgrounds, effects and tilt", () => {
    const result = settingsFor("balanced", {
        augmentation: 0.4,
        backgrounds: ["notebook"],
        layers: { grain: 0.6, stains: 0, shadow: null },
        rotation: 5,
        blur: 0,
        contrast: 0.1,
    });
    assert.equal(result.augmentation, 0.4);
    assert.deepEqual([...result.background_presets], ["notebook"]);
    assert.deepEqual({ ...result.background_weights }, {});
    assert.equal(result.grain, 0.6);
    assert.equal(result.stains, 0);
    // Auto sends null, so the background's own amount is used.
    assert.equal(result.shadow, null);
    assert.equal(result.rotation, 5);
    assert.equal(result.blur, 0);
    assert.equal(result.contrast, 0.1);
});

test("controls show the level closest to a preset's value", () => {
    const closest = appFunctions(
        "function closestLevel(",
        "function readLevel(",
        { Math },
        "closestLevel",
    );
    const effects = [["auto"], ["0"], ["0.2"], ["0.6"]];
    assert.equal(closest(effects, null), "auto");
    assert.equal(closest(effects, 0.25), "0.2");
    assert.equal(closest(effects, 0.8), "0.6");
    assert.equal(closest([["0"], ["2"], ["5"]], 2), "2");
});

test("language shares are sent only when several languages are selected", () => {
    assert.deepEqual({ ...settingsFor("balanced").language_weights }, {});
    const result = settingsFor("balanced", null, { ckb: 70, kmr: 30 });
    assert.deepEqual({ ...result.language_weights }, { ckb: 70, kmr: 30 });
});

function languageControls(values) {
    const inputs = values.map((value) => ({ value }));
    return appFunctions(
        "function selectedLanguages()",
        "// Whole numbers that add up",
        {
            document: {
                querySelectorAll: () => inputs,
                querySelector: () => null,
            },
            LANGUAGE_TAGS: {
                sorani: "ckb",
                kurmanji: "kmr",
                ar: "ar",
                en: "en",
            },
            state: { fileLanguage: null },
            $: () => ({ files: [] }),
            Intl,
        },
        "({selectedLanguages, selectedLanguage, languageError, selectionName})",
    );
}

test("Kurdish can include Arabic and English without selecting unrelated languages", () => {
    const controls = languageControls(["sorani", "kurmanji", "ar", "en", "fa"]);
    assert.deepEqual(
        [...controls.selectedLanguages()],
        ["ckb", "kmr", "ar", "en"],
    );
    assert.equal(controls.languageError(), "");
    assert.equal(
        controls.selectionName(controls.selectedLanguages()),
        "Sorani + Kurmanji + Arabic + English",
    );
    assert.equal(
        languageControls(["sorani", "kurmanji"]).selectedLanguage(),
        "both",
    );
});

test("a TXT upload with several languages needs its language chosen, never guessed", () => {
    const inputs = [{ value: "sorani" }, { value: "en" }];
    const state = { fileLanguage: null };
    let fileName = "lines.txt";
    const controls = appFunctions(
        "function selectedLanguages()",
        "// Whole numbers that add up",
        {
            document: {
                querySelectorAll: () => inputs,
                querySelector: () => ({ value: "upload" }),
            },
            LANGUAGE_TAGS: {
                sorani: "ckb",
                kurmanji: "kmr",
                ar: "ar",
                en: "en",
            },
            state,
            $: () => ({ files: [{ name: fileName }] }),
            Intl,
        },
        "({datasetLanguages, languageError, languageParameter})",
    );
    assert.deepEqual([...controls.datasetLanguages()], []);
    assert.match(controls.languageError(), /language of your text file/);
    state.fileLanguage = "en";
    assert.deepEqual([...controls.datasetLanguages()], ["en"]);
    assert.equal(controls.languageError(), "");
    assert.equal(controls.languageParameter(["ckb"]), "sorani");
    // CSV and JSONL carry a language column, so every selected language is kept.
    fileName = "lines.csv";
    assert.deepEqual([...controls.datasetLanguages()], ["ckb", "en"]);
    inputs.pop();
    fileName = "lines.txt";
    assert.deepEqual([...controls.datasetLanguages()], ["ckb"]);
});

test("selecting no language is a visible error", () => {
    assert.match(languageControls([]).languageError(), /Select/);
});

function shareFunctions() {
    return appFunctions(
        "// Whole numbers that add up",
        "function updateShareCounts()",
        { Math, Object },
        "({divideShares, rebalanceShares})",
    );
}

test("language shares start even and always total 100", () => {
    const { divideShares } = shareFunctions();
    assert.deepEqual(
        { ...divideShares(["ckb", "kmr", "ar"], {}) },
        {
            ckb: 34,
            kmr: 33,
            ar: 33,
        },
    );
    assert.deepEqual(
        { ...divideShares(["a", "b"], { a: 3, b: 1 }, 10) },
        {
            a: 8,
            b: 2,
        },
    );
});

test("the share edited just before stays put", () => {
    const { rebalanceShares } = shareFunctions();
    const names = ["ckb", "kmr", "en"];
    const first = rebalanceShares(
        { ckb: 34, kmr: 33, en: 33 },
        names,
        "ckb",
        50,
    );
    assert.deepEqual({ ...first }, { ckb: 50, kmr: 25, en: 25 });
    // Setting Kurmanji next keeps Sorani at 50; English takes the rest.
    assert.deepEqual(
        { ...rebalanceShares(first, names, "kmr", 30, "ckb") },
        {
            ckb: 50,
            kmr: 30,
            en: 20,
        },
    );
    // If the earlier share no longer fits, everything else adjusts instead.
    const total = rebalanceShares(first, names, "kmr", 80, "ckb");
    assert.equal(
        Object.values(total).reduce((a, b) => a + b, 0),
        100,
    );
    assert.equal(total.kmr, 80);
    // With two languages the other one always takes the rest.
    assert.deepEqual(
        { ...rebalanceShares({ a: 60, b: 40 }, ["a", "b"], "b", 10, "a") },
        {
            a: 90,
            b: 10,
        },
    );
});

test("changing one share keeps the others in proportion", () => {
    const { rebalanceShares } = shareFunctions();
    const names = ["ckb", "kmr", "en"];
    const shares = { ckb: 50, kmr: 30, en: 20 };
    assert.deepEqual(
        { ...rebalanceShares(shares, names, "ckb", 80) },
        {
            ckb: 80,
            kmr: 12,
            en: 8,
        },
    );
    const total = (value) => Object.values(value).reduce((a, b) => a + b, 0);
    for (const value of [0, 1, 33, 99, 100, 150, -5])
        assert.equal(total(rebalanceShares(shares, names, "kmr", value)), 100);
    assert.deepEqual(
        { ...rebalanceShares({ a: 100, b: 0 }, ["a", "b"], "a", 60) },
        {
            a: 60,
            b: 40,
        },
    );
});

test("the preview rotates through a style's backgrounds a few at a time", () => {
    const state = { previewIteration: 0 };
    const pick = appFunctions(
        "// Show a few",
        "function previewTile(",
        { state, PREVIEW_EXAMPLES: 3 },
        "previewBackgrounds",
    );
    const all = ["a", "b", "c", "d", "e"];
    assert.deepEqual([...pick({ background_presets: all })], ["a", "b", "c"]);
    state.previewIteration = 1;
    assert.deepEqual([...pick({ background_presets: all })], ["d", "e", "a"]);
    assert.deepEqual([...pick({ background_presets: ["x"] })], ["x"]);
    assert.deepEqual([...pick({})], []);
});

function downloadControls(status = "completed") {
    const nodes = new Map();
    const $ = (id) => {
        if (!nodes.has(id)) nodes.set(id, {});
        return nodes.get(id);
    };
    const state = { job: { id: "example", status }, downloadPending: false };
    const requests = [],
        links = [];
    const download = appFunctions(
        "function safeDownload(",
        "const CRC_TABLE",
        {
            state,
            $,
            encodeURIComponent,
            selectedFormat: () => "dataset",
            post: (url, body) =>
                new Promise((resolve, reject) =>
                    requests.push({ url, body, resolve, reject }),
                ),
            document: {
                body: { append: (link) => links.push(link) },
                createElement: (tag) => {
                    assert.equal(tag, "a");
                    return {
                        click() {
                            this.clicked = true;
                        },
                        remove() {
                            this.removed = true;
                        },
                    };
                },
            },
        },
        "downloadDataset",
    );
    return { state, requests, links, $, download };
}

test("downloads prepare first, then use the browser without fetching dataset bytes", async () => {
    const controls = downloadControls();
    const pending = controls.download("dataset");
    assert.equal(controls.requests.length, 1);
    assert.equal(controls.links.length, 0);
    assert.equal(controls.$("download-button").disabled, true);
    await controls.download("csv");
    assert.equal(
        controls.requests.length,
        1,
        "repeat clicks do not queue more exports",
    );
    const url = "/api/jobs/example/download?format=dataset";
    assert.equal(controls.requests[0].url, url);
    controls.requests[0].resolve({ url, filename: "training-dataset.zip" });
    await pending;
    assert.equal(controls.links.length, 1);
    assert.equal(controls.links[0].href, url);
    assert.equal(controls.links[0].download, "training-dataset.zip");
    assert.ok(controls.links[0].clicked && controls.links[0].removed);
    assert.equal(controls.$("download-button").disabled, false);
    assert.match(controls.$("download-status").textContent, /Download started/);
});

test("download errors stay on the page and a retry remains possible", async () => {
    const controls = downloadControls();
    const pending = controls.download("dataset");
    controls.requests[0].reject(new Error("Try fewer images"));
    await pending;
    assert.equal(controls.links.length, 0);
    assert.equal(controls.$("download-error").hidden, false);
    assert.equal(controls.$("download-error").textContent, "Try fewer images");
    assert.equal(controls.state.downloadPending, false);
    assert.equal(controls.$("download-button").disabled, false);
});

test("unverified data and unexpected download URLs never trigger browser downloads", async () => {
    for (const status of ["generating", "failed", "cancelled"]) {
        const controls = downloadControls(status);
        await controls.download("dataset");
        assert.equal(controls.requests.length, 0);
    }
    const controls = downloadControls();
    const pending = controls.download("manifest");
    controls.requests[0].resolve({
        url: "https://elsewhere.invalid/data",
        filename: "data.zip",
    });
    await pending;
    assert.equal(controls.links.length, 0);
    assert.equal(controls.$("download-error").hidden, false);
});

function folderFunctions() {
    return appFunctions(
        "const CRC_TABLE",
        "async function saveToFolder(",
        { TextDecoder, Uint8Array, DataView, Number, Map, Error },
        "({crc32, extractStoredZip, newDatasetFolder, safeArchivePath})",
    );
}

// An in-memory stand-in for the browser's FileSystemDirectoryHandle.
function memoryFolder(name = "Chosen") {
    const entries = new Map();
    return {
        name,
        entries,
        async getDirectoryHandle(child, { create = false } = {}) {
            if (!entries.has(child)) {
                if (!create)
                    throw Object.assign(new Error("missing"), {
                        name: "NotFoundError",
                    });
                entries.set(child, memoryFolder(child));
            }
            return entries.get(child);
        },
        async getFileHandle(child) {
            return {
                async createWritable() {
                    const parts = [];
                    return {
                        write: async (piece) => parts.push(Buffer.from(piece)),
                        close: async () =>
                            entries.set(child, Buffer.concat(parts)),
                        abort: async () => {},
                    };
                },
            };
        },
        async *keys() {
            yield* entries.keys();
        },
    };
}

// Matches synthocr.web._stored_zip: stored entries with ZIP64 sizes up front.
function storedZip(files, { crc } = {}) {
    const { crc32 } = folderFunctions();
    const parts = [];
    for (const [name, text] of files) {
        const data = Buffer.from(text, "utf8");
        const encoded = Buffer.from(name, "utf8");
        const header = Buffer.alloc(30);
        header.writeUInt32LE(0x04034b50, 0);
        header.writeUInt16LE(45, 4);
        header.writeUInt16LE(0x800, 6);
        header.writeUInt16LE(33, 12);
        header.writeUInt32LE(crc ?? crc32(data), 14);
        header.writeUInt32LE(0xffffffff, 18);
        header.writeUInt32LE(0xffffffff, 22);
        header.writeUInt16LE(encoded.length, 26);
        header.writeUInt16LE(20, 28);
        const extra = Buffer.alloc(20);
        extra.writeUInt16LE(1, 0);
        extra.writeUInt16LE(16, 2);
        extra.writeBigUInt64LE(BigInt(data.length), 4);
        extra.writeBigUInt64LE(BigInt(data.length), 12);
        parts.push(header, encoded, extra, data);
    }
    const central = Buffer.alloc(8);
    central.writeUInt32LE(0x02014b50, 0);
    parts.push(central);
    return Buffer.concat(parts);
}

function streamOf(bytes, size = 7) {
    let offset = 0;
    return new ReadableStream({
        pull(controller) {
            if (offset >= bytes.length) return controller.close();
            controller.enqueue(
                new Uint8Array(bytes.subarray(offset, offset + size)),
            );
            offset += size;
        },
    });
}

test("a stored ZIP is written file by file into nested folders", async () => {
    const { extractStoredZip } = folderFunctions();
    const folder = memoryFolder();
    const written = [];
    const count = await extractStoredZip(
        streamOf(
            storedZip([
                ["manifest.jsonl", '{"text":"سڵاو 2024"}\n'],
                ["images/00000000.png", "png-bytes"],
                ["images/00000001.png", ""],
            ]),
        ),
        folder,
        (index, name) => written.push(name),
    );
    assert.equal(count, 3);
    assert.deepEqual(written, [
        "manifest.jsonl",
        "images/00000000.png",
        "images/00000001.png",
    ]);
    assert.equal(
        folder.entries.get("manifest.jsonl").toString("utf8"),
        '{"text":"سڵاو 2024"}\n',
    );
    const images = folder.entries.get("images").entries;
    assert.equal(images.get("00000000.png").toString(), "png-bytes");
    assert.equal(images.get("00000001.png").length, 0);
});

test("damaged, truncated or escaping archives are refused", async () => {
    const { extractStoredZip } = folderFunctions();
    await assert.rejects(
        extractStoredZip(
            streamOf(storedZip([["a.txt", "x"]], { crc: 1 })),
            memoryFolder(),
        ),
        /damaged/,
    );
    const whole = storedZip([["a.txt", "hello"]]);
    await assert.rejects(
        extractStoredZip(
            streamOf(whole.subarray(0, whole.length - 12)),
            memoryFolder(),
        ),
        /ended early/,
    );
    for (const name of ["../escape.txt", "/root.txt", "a\\b.txt", "a//b.txt"])
        await assert.rejects(
            extractStoredZip(
                streamOf(storedZip([[name, "x"]])),
                memoryFolder(),
            ),
            /unsafe file name/,
        );
});

test("a dataset never overwrites a nonempty folder", async () => {
    const { newDatasetFolder } = folderFunctions();
    const parent = memoryFolder("Datasets");
    const created = await newDatasetFolder(parent, "run-1");
    assert.equal(created.name, "run-1");
    assert.equal(await newDatasetFolder(parent, "run-1"), created);
    created.entries.set("old.txt", Buffer.from("keep"));
    await assert.rejects(newDatasetFolder(parent, "run-1"), /already exists/);
    assert.equal(created.entries.get("old.txt").toString(), "keep");
});

function fontControls(catalog, choice, languages) {
    return appFunctions(
        "// null means every font",
        "function updateFontSummary()",
        {
            state: { fontCatalog: catalog, fontChoice: choice },
            datasetLanguages: () => languages,
            languageName: (tag) => ({ ckb: "Sorani", kmr: "Kurmanji" })[tag],
        },
        "({chosenFontIds, chosenFonts, fontError, fontWarning})",
    );
}

test("all fonts are used until the user narrows the choice", () => {
    const catalog = [
        { id: "a:0", languages: ["ckb", "ar"] },
        { id: "b:0", languages: ["kmr", "en"] },
    ];
    const all = fontControls(catalog, null, ["ckb"]);
    assert.deepEqual([...all.chosenFontIds()], []);
    assert.equal(all.chosenFonts().length, 2);
    const one = fontControls(catalog, new Set(["b:0"]), ["ckb", "kmr"]);
    assert.deepEqual([...one.chosenFontIds()], ["b:0"]);
    assert.match(one.fontWarning(), /Sorani/);
    assert.doesNotMatch(one.fontWarning(), /Kurmanji/);
    assert.equal(
        fontControls(catalog, new Set(), ["ckb"]).fontError(),
        "Choose at least one font.",
    );
    assert.equal(fontControls([], null, ["ckb"]).fontError(), "");
});
