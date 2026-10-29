"use strict";

const $ = (id) => document.getElementById(id);
const icons = {
    "arrow-right": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    chevron: '<path d="m7 10 5 5 5-5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    refresh:
        '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.2 7a7 7 0 0 1 11.5-1L20 9M4 15l2.3 3A7 7 0 0 0 17.8 17"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3V7Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    moon: '<path d="M20 14A8.5 8.5 0 0 1 10 4a8.5 8.5 0 1 0 10 10Z"/>',
};

function icon(name) {
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || ""}</svg>`;
}

function mountIcons(root = document) {
    root.querySelectorAll("[data-icon]").forEach((node) => {
        node.innerHTML = icon(node.dataset.icon);
    });
}

mountIcons();

const state = {
    info: null,
    job: null,
    runningId: null,
    pollTimer: null,
    previewTimer: null,
    previewController: null,
    previewSequence: 0,
    previewIteration: Math.floor(Math.random() * 32),
    galleryOffset: 0,
    gallerySequence: 0,
    galleryController: null,
    galleryJob: null,
    jobs: [],
    toastTimer: null,
    nameEdited: false,
    samplesEdited: false,
    sourceMode: "corpus",
    upload: null,
    uploadController: null,
    uploadSequence: 0,
    uploadPending: false,
    downloadPending: false,
    languageShares: {},
    lastShareEdited: null,
    fileLanguage: null,
    style: "balanced",
    custom: null,
    folder: null,
    fontCatalog: [],
    fontChoice: null,
    fontPending: false,
    saveTargets: new Map(),
    saving: false,
};
const terminal = new Set(["completed", "failed", "cancelled"]);
const PREVIEW_EXAMPLES = 3;
const GALLERY_PAGE = 6;
const LANGUAGE_TAGS = { sorani: "ckb", kurmanji: "kmr", ar: "ar", en: "en" };
// Chrome and Edge can write into a folder the user picks; other browsers download a ZIP.
const canChooseFolder = typeof window.showDirectoryPicker === "function";

async function api(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...options.headers },
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    return response.json();
}

async function errorMessage(response) {
    let message = `Request failed (${response.status}). Please try again.`;
    try {
        const body = await response.json();
        if (typeof body.detail === "string") message = body.detail;
        else if (Array.isArray(body.detail)) {
            message = body.detail
                .map(
                    (error) =>
                        `${error.loc?.slice(1).join(".") || "Settings"}: ${error.msg}`,
                )
                .join(". ");
        } else if (body.detail && typeof body.detail.message === "string") {
            message = body.detail.message;
        } else if (body.error) message = String(body.error);
    } catch {
        /* A failed request may return a non-JSON response. */
    }
    return message;
}

function post(path, body, options = {}) {
    return api(path, {
        method: "POST",
        body: JSON.stringify(body),
        ...options,
    });
}

function selectedLanguages() {
    return [...document.querySelectorAll('input[name="language"]:checked')]
        .map((input) => LANGUAGE_TAGS[input.value])
        .filter(Boolean);
}

function selectedLanguage() {
    return languageParameter(selectedLanguages());
}

// The legacy single-value form of a language selection.
function languageParameter(languages) {
    if (
        languages.length === 2 &&
        languages.includes("ckb") &&
        languages.includes("kmr")
    )
        return "both";
    return (
        { ckb: "sorani", kmr: "kurmanji" }[languages[0]] || languages[0] || ""
    );
}

function selectedSource() {
    return (
        document.querySelector('input[name="source"]:checked')?.value ||
        "corpus"
    );
}

function selectedFormat() {
    return (
        document.querySelector('input[name="download-format"]:checked')
            ?.value || "dataset"
    );
}

// A TXT file has no language column, so with several languages selected the
// user says which one it is. The app never guesses a language from the script.
function needsFileLanguage() {
    return (
        selectedSource() === "upload" &&
        /\.txt$/i.test($("corpus-file").files?.[0]?.name || "") &&
        selectedLanguages().length > 1
    );
}

// The languages the dataset will actually contain.
function datasetLanguages() {
    if (needsFileLanguage())
        return state.fileLanguage ? [state.fileLanguage] : [];
    return selectedLanguages();
}

function languageError() {
    if (!selectedLanguages().length) return "Select at least one language.";
    if (needsFileLanguage() && !state.fileLanguage)
        return "Choose the language of your text file.";
    return "";
}

function languageName(language) {
    const known = {
        sorani: "Sorani",
        ckb: "Sorani",
        kurmanji: "Kurmanji",
        kmr: "Kurmanji",
        both: "Sorani + Kurmanji",
        ar: "Arabic",
        en: "English",
    };
    if (known[language]) return known[language];
    try {
        return (
            new Intl.DisplayNames(["en"], { type: "language" }).of(language) ||
            language
        );
    } catch {
        return language;
    }
}

function selectionName(languages, fallback) {
    return languages?.length
        ? languages.map(languageName).join(" + ")
        : languageName(fallback);
}

// Whole numbers that add up to exactly total, in proportion to the weights
// (equal when every weight is zero).
function divideShares(names, weights, total = 100) {
    if (!names.length) return {};
    const values = names.map((name) => Math.max(0, weights[name] ?? 0));
    const sum = values.reduce((a, b) => a + b, 0);
    const exact = values.map((value) =>
        sum ? (total * value) / sum : total / names.length,
    );
    const shares = exact.map(Math.floor);
    const order = names
        .map((_, index) => index)
        .sort((a, b) => exact[b] - shares[b] - (exact[a] - shares[a]) || a - b);
    const remainder = total - shares.reduce((a, b) => a + b, 0);
    for (const index of order.slice(0, remainder)) shares[index]++;
    return Object.fromEntries(
        names.map((name, index) => [name, shares[index]]),
    );
}

// Set one language's share. The share edited just before stays put when it
// can, and the remaining languages keep their proportions in what is left.
function rebalanceShares(shares, names, changed, value, previous = null) {
    const fixed = Math.max(0, Math.min(100, Math.round(value) || 0));
    const others = names.filter((name) => name !== changed);
    if (!others.length) return { [changed]: 100 };
    const kept =
        previous !== changed &&
        others.includes(previous) &&
        others.length > 1 &&
        shares[previous] <= 100 - fixed
            ? previous
            : null;
    const adjusted = others.filter((name) => name !== kept);
    const rest = 100 - fixed - (kept ? shares[kept] : 0);
    return {
        ...divideShares(adjusted, shares, rest),
        ...(kept ? { [kept]: shares[kept] } : {}),
        [changed]: fixed,
    };
}

function updateShareCounts() {
    const total = Math.max(0, Number($("samples").value) || 0);
    for (const tag of datasetLanguages()) {
        const count = document.querySelector(`[data-share-count="${tag}"]`);
        if (count)
            count.textContent = `≈ ${Math.round((total * (state.languageShares[tag] || 0)) / 100).toLocaleString()} images`;
    }
}

function renderFileLanguage() {
    const tags = selectedLanguages();
    if (!tags.includes(state.fileLanguage)) state.fileLanguage = null;
    const show = needsFileLanguage();
    $("file-language-field").hidden = !show;
    const options = $("file-language-options");
    options.replaceChildren();
    if (!show) return;
    for (const tag of tags) {
        const label = document.createElement("label");
        label.className = "chip";
        const input = document.createElement("input");
        input.type = "radio";
        input.name = "file-language";
        input.value = tag;
        input.checked = tag === state.fileLanguage;
        const text = document.createElement("span");
        text.textContent = languageName(tag);
        label.append(input, text);
        options.append(label);
    }
}

function renderLanguageShares() {
    const tags = datasetLanguages();
    const known = Object.keys(state.languageShares);
    if (
        known.length !== tags.length ||
        tags.some((tag) => !known.includes(tag))
    ) {
        state.languageShares = divideShares(tags, {});
        state.lastShareEdited = null;
    }
    $("language-shares-field").hidden = tags.length < 2;
    const list = $("language-shares");
    list.replaceChildren();
    if (tags.length < 2) return;
    for (const tag of tags) {
        const row = document.createElement("div");
        row.className = "share-row";
        const name = document.createElement("label");
        name.className = "share-name";
        name.htmlFor = `share-${tag}`;
        name.textContent = languageName(tag);
        const input = document.createElement("input");
        input.type = "number";
        input.id = `share-${tag}`;
        input.min = "0";
        input.max = "100";
        input.step = "1";
        input.inputMode = "numeric";
        input.value = String(state.languageShares[tag]);
        input.dataset.share = tag;
        input.addEventListener("input", () => {
            if (!Number.isFinite(input.valueAsNumber)) return;
            state.languageShares = rebalanceShares(
                state.languageShares,
                tags,
                tag,
                input.valueAsNumber,
                state.lastShareEdited,
            );
            for (const other of list.querySelectorAll("[data-share]"))
                if (other !== input)
                    other.value = String(
                        state.languageShares[other.dataset.share],
                    );
            updateShareCounts();
        });
        input.addEventListener("change", () => {
            input.value = String(state.languageShares[tag]);
            state.lastShareEdited = tag;
        });
        const percent = document.createElement("span");
        percent.className = "share-percent";
        percent.textContent = "%";
        const count = document.createElement("span");
        count.className = "share-count";
        count.dataset.shareCount = tag;
        row.append(name, input, percent, count);
        list.append(row);
    }
    updateShareCounts();
}

// A style is a server preset; Customize overrides a few of its fields.
function appearanceSettings() {
    const base = {
        ...(state.info?.image_presets?.[state.style]?.settings || {}),
    };
    const custom = state.custom;
    if (!custom) return base;
    return {
        ...base,
        augmentation: custom.augmentation,
        background_presets: [...custom.backgrounds],
        background_weights: {},
        ...custom.layers,
        rotation: custom.rotation,
        blur: custom.blur,
        contrast: custom.contrast,
    };
}

// Each effect is Auto (the background's own amount), off, or a fixed strength.
const EFFECT_LEVELS = [
    ["auto", "Auto"],
    ["0", "Off"],
    ["0.2", "Light"],
    ["0.6", "Strong"],
];
// Sliders for the variation limits; each image draws a random amount up to them.
const GEOMETRY = {
    rotation: { id: "tilt-amount", show: (value) => `${value}°` },
    blur: { id: "blur-amount", show: (value) => `${value} px` },
    contrast: {
        id: "contrast-amount",
        show: (value) => `${Math.round(value * 100)}%`,
        scale: 100,
    },
};

function readGeometry(name) {
    const item = GEOMETRY[name];
    return Number($(item.id).value) / (item.scale || 1);
}

function showGeometry(name, value) {
    const item = GEOMETRY[name];
    $(item.id).value = String(value * (item.scale || 1));
    $(`${item.id}-value`).textContent = item.show(value);
}

function closestLevel(levels, value) {
    if (value == null) return "auto";
    return levels
        .map(([level]) => level)
        .filter((level) => level !== "auto")
        .reduce((best, level) =>
            Math.abs(level - value) < Math.abs(best - value) ? level : best,
        );
}

function readLevel(name) {
    const value = document.querySelector(
        `input[name="effect-${name}"]:checked`,
    )?.value;
    return value === undefined || value === "auto" ? null : Number(value);
}

function effectRow(container, name, label, description, levels) {
    const row = document.createElement("div");
    row.className = "effect-row";
    const text = document.createElement("span");
    text.className = "effect-text";
    const title = document.createElement("strong");
    title.textContent = label;
    const detail = document.createElement("small");
    detail.textContent = description;
    text.append(title, detail);
    const group = document.createElement("div");
    group.className = "segmented";
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", label);
    for (const [value, caption] of levels) {
        const option = document.createElement("label");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = `effect-${name}`;
        input.value = value;
        const span = document.createElement("span");
        span.textContent = caption;
        option.append(input, span);
        group.append(option);
    }
    row.append(text, group);
    container.append(row);
}

function mountEffectOptions(layers) {
    $("effect-options").replaceChildren();
    for (const layer of layers)
        effectRow(
            $("effect-options"),
            layer.id,
            layer.label,
            layer.description,
            EFFECT_LEVELS,
        );
}

function effectLayers() {
    return (state.info?.document_layers || []).map((layer) => layer.id);
}

function currentSettings() {
    const source = selectedSource();
    const languages = datasetLanguages();
    return {
        name: $("dataset-name").value.trim(),
        language: languageParameter(languages),
        languages,
        language_weights:
            languages.length > 1 ? { ...state.languageShares } : {},
        source,
        upload_id: source === "upload" ? state.upload?.id || null : null,
        samples: Number($("samples").value),
        font_size: Number($("text-size").value),
        fonts: chosenFontIds(),
        ...appearanceSettings(),
        // One dataset: every image is in the same set, with no held-out split.
        train_ratio: 1,
        validation_ratio: 0,
    };
}

function randomSeed() {
    const values = new Uint32Array(1);
    crypto.getRandomValues(values);
    return values[0];
}

function mountBackgroundOptions(catalog) {
    const container = $("background-options");
    container.replaceChildren();
    for (const [name, preset] of Object.entries(catalog)) {
        const label = document.createElement("label");
        label.className = "chip";
        label.title = preset.description || "";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = "background";
        input.value = name;
        const text = document.createElement("span");
        text.textContent = preset.label;
        label.append(input, text);
        container.append(label);
    }
}

function showStyleControls() {
    const settings = appearanceSettings();
    const amount = Math.round((settings.augmentation || 0) * 100);
    $("effects-amount").value = String(amount);
    $("effects-amount-value").textContent = `${amount}%`;
    const selected = settings.background_presets || [];
    for (const input of document.querySelectorAll('input[name="background"]'))
        input.checked = selected.includes(input.value);
    for (const name of effectLayers()) {
        const value = closestLevel(EFFECT_LEVELS, settings[name] ?? null);
        const input = document.querySelector(
            `input[name="effect-${name}"][value="${value}"]`,
        );
        if (input) input.checked = true;
    }
    for (const name of Object.keys(GEOMETRY))
        showGeometry(name, Number(settings[name] ?? 0));
    for (const id of ["backgrounds-field", "effects-field", "geometry-field"])
        $(id).disabled = amount === 0;
    for (const input of document.querySelectorAll('input[name="style"]'))
        input.checked = !state.custom && input.value === state.style;
    $("style-status").hidden = !state.custom;
}

function readCustomControls() {
    const augmentation = Number($("effects-amount").value) / 100;
    let backgrounds = [
        ...document.querySelectorAll('input[name="background"]:checked'),
    ].map((input) => input.value);
    // Images with effects need at least one background to draw on.
    if (augmentation > 0 && !backgrounds.length)
        backgrounds = state.info?.image_presets?.balanced?.settings
            .background_presets || [
            document.querySelector('input[name="background"]')?.value,
        ];
    state.custom = {
        augmentation,
        backgrounds: backgrounds.filter(Boolean),
        layers: Object.fromEntries(
            effectLayers().map((name) => [name, readLevel(name)]),
        ),
        rotation: readGeometry("rotation"),
        blur: readGeometry("blur"),
        contrast: readGeometry("contrast"),
    };
    showStyleControls();
    schedulePreview();
}

// null means every font; otherwise the set of chosen font IDs.
function chosenFontIds() {
    return state.fontChoice ? [...state.fontChoice] : [];
}

function chosenFonts() {
    return state.fontCatalog.filter(
        (font) => !state.fontChoice || state.fontChoice.has(font.id),
    );
}

function fontError() {
    if (state.fontCatalog.length && !chosenFonts().length)
        return "Choose at least one font.";
    return "";
}

function fontWarning() {
    const chosen = chosenFonts();
    const missing = datasetLanguages().filter(
        (tag) => !chosen.some((font) => font.languages.includes(tag)),
    );
    return chosen.length && missing.length
        ? `None of the chosen fonts can draw all of ${missing.map(languageName).join(" or ")}. Images in that language may fail.`
        : "";
}

function updateFontSummary() {
    const total = state.fontCatalog.length;
    const count = chosenFonts().length;
    $("fonts-summary").textContent = state.fontChoice
        ? `${count} of ${total}`
        : `All ${total}`;
    const message = fontError() || fontWarning();
    $("font-error").textContent = message;
    $("font-error").hidden = !message;
}

function renderFonts() {
    const list = $("font-list");
    list.replaceChildren();
    for (const font of state.fontCatalog) {
        const row = document.createElement("div");
        row.className = "font-row";
        const label = document.createElement("label");
        label.title = font.file;
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = font.id;
        input.checked = !state.fontChoice || state.fontChoice.has(font.id);
        const name = document.createElement("span");
        name.className = "font-name";
        name.textContent = font.name;
        const tags = document.createElement("span");
        tags.className = "font-tags";
        tags.textContent = font.languages.map(languageName).join(" · ");
        label.append(input, name, tags);
        row.append(label);
        if (font.user) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "text-button";
            remove.textContent = "Remove";
            remove.setAttribute("aria-label", `Remove ${font.name}`);
            remove.addEventListener("click", () => removeFont(font));
            row.append(remove);
        }
        list.append(row);
    }
    updateFontSummary();
}

async function loadFonts() {
    const result = await api("/api/fonts");
    state.fontCatalog = result.fonts;
    if (state.fontChoice?.size) {
        const known = new Set(state.fontCatalog.map((font) => font.id));
        const kept = [...state.fontChoice].filter((id) => known.has(id));
        // If every chosen font was removed, go back to using all fonts.
        state.fontChoice = kept.length ? new Set(kept) : null;
    }
    renderFonts();
}

function setFontChoice(choice) {
    state.fontChoice =
        choice && choice.size < state.fontCatalog.length ? choice : null;
    renderFonts();
    schedulePreview();
    setBusy();
}

async function addFonts() {
    const files = [...$("font-file").files];
    $("font-file").value = "";
    if (!files.length) return;
    state.fontPending = true;
    setBusy();
    const added = [];
    const known = [];
    try {
        for (const file of files) {
            $("font-status").textContent = `Adding ${file.name}…`;
            const result = await api(
                `/api/fonts?filename=${encodeURIComponent(file.name)}`,
                {
                    method: "POST",
                    body: file,
                    headers: { "Content-Type": "application/octet-stream" },
                },
            );
            (result.added ? added : known).push(...result.fonts);
        }
        await loadFonts();
        if (state.fontChoice)
            setFontChoice(
                new Set([
                    ...state.fontChoice,
                    ...[...added, ...known].map((font) => font.id),
                ]),
            );
        $("font-status").textContent = [
            added.length
                ? `Added ${added.map((font) => font.name).join(", ")}.`
                : "",
            known.length ? "Some files were already available." : "",
        ]
            .filter(Boolean)
            .join(" ");
        schedulePreview();
    } catch (error) {
        $("font-status").textContent = error.message;
    } finally {
        state.fontPending = false;
        setBusy();
    }
}

async function removeFont(font) {
    try {
        await api(`/api/fonts/${encodeURIComponent(font.id.split(":")[0])}`, {
            method: "DELETE",
        });
        $("font-status").textContent = `Removed ${font.name}.`;
        await loadFonts();
        schedulePreview();
        setBusy();
    } catch (error) {
        $("font-status").textContent = error.message;
    }
}

function updateThemeButton() {
    const dark = document.documentElement.dataset.theme !== "light";
    $("theme-label").textContent = dark ? "Light mode" : "Dark mode";
    $("theme-toggle").setAttribute(
        "aria-label",
        `Switch to ${dark ? "light" : "dark"} mode`,
    );
    $("theme-toggle").querySelector("[data-icon]").innerHTML = icon(
        dark ? "sun" : "moon",
    );
}

function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => {
        $("toast").hidden = true;
    }, 3500);
}

function setError(message = "") {
    $("form-error").textContent = message;
    $("form-error").hidden = !message;
}

function suggestName() {
    if (state.nameEdited) return;
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
    const time = `${pad(now.getHours())}${pad(now.getMinutes())}`;
    const chosen = datasetLanguages();
    const languages = chosen.length ? chosen : selectedLanguages();
    const prefix = languages.some((tag) => ["ckb", "kmr"].includes(tag))
        ? languages.length === 1
            ? languageParameter(languages)
            : "kurdish"
        : languages.join("-") || "dataset";
    const base = `${prefix.slice(0, 32)}-${date}-${time}`;
    let name = base;
    let suffix = 2;
    while (state.jobs.some((job) => job.name === name))
        name = `${base}-${suffix++}`;
    $("dataset-name").value = name;
    updateFolder();
}

function updateFolder() {
    $("choose-folder").hidden = !canChooseFolder;
    $("folder-name").textContent = state.folder
        ? state.folder.name
        : canChooseFolder
          ? "No folder chosen"
          : "Your browser's downloads";
    const name = $("dataset-name").value.trim() || "dataset";
    $("folder-status").textContent = !canChooseFolder
        ? "This browser cannot save into a folder; use Chrome or Edge for that. You can download a ZIP when the dataset is ready."
        : state.folder
          ? `Saved as ${state.folder.name}/${name} when it is ready.`
          : "Choose a folder on your computer. The dataset is saved there when it is ready.";
}

async function chooseFolder() {
    try {
        state.folder = await window.showDirectoryPicker({
            id: "synthocr-datasets",
            mode: "readwrite",
        });
        updateFolder();
        return true;
    } catch (error) {
        if (error.name !== "AbortError") setError(error.message);
        return false;
    }
}

function updateSource() {
    const source = selectedSource();
    if (state.sourceMode === "upload" && source !== "upload") resetUpload();
    state.sourceMode = source;
    $("upload-field").hidden = source !== "upload";
    const error = selectedLanguages().length ? "" : languageError();
    $("language-help").textContent = error;
    $("language-help").hidden = !error;
    setError();
    suggestName();
    renderFileLanguage();
    renderLanguageShares();
    if (state.fontCatalog.length) updateFontSummary();
    schedulePreview();
    setBusy();
}

async function discardUpload(upload) {
    if (upload && !upload.consumed) {
        try {
            await api(`/api/uploads/${encodeURIComponent(upload.id)}`, {
                method: "DELETE",
            });
        } catch {
            /* Unused uploads also expire automatically. */
        }
    }
}

function resetUpload() {
    state.uploadController?.abort();
    state.uploadSequence++;
    state.uploadPending = false;
    const previous = state.upload;
    state.upload = null;
    discardUpload(previous);
}

async function uploadSelectedFile() {
    state.uploadController?.abort();
    const sequence = ++state.uploadSequence;
    const previous = state.upload;
    state.upload = null;
    state.uploadPending = true;
    setBusy();
    await discardUpload(previous);
    if (sequence !== state.uploadSequence) return;
    const file = $("corpus-file").files[0];
    const status = $("upload-status");
    status.hidden = false;
    state.uploadPending = false;
    if (!file) {
        status.textContent = "Choose a file to upload.";
        setBusy();
        return;
    }
    const limit = state.info?.upload_limit_bytes || 50 * 1024 * 1024;
    const error = languageError();
    if (error) {
        status.textContent = error;
        setBusy();
        return;
    }
    if (
        !/\.(txt|jsonl|csv)$/i.test(file.name) ||
        file.size > limit ||
        !file.size
    ) {
        status.textContent = `Choose a UTF-8 TXT, CSV or JSONL file up to ${Math.round(limit / 1048576)} MB.`;
        setBusy();
        return;
    }
    const controller = new AbortController();
    state.uploadController = controller;
    state.uploadPending = true;
    status.textContent = `Uploading ${file.name}…`;
    setBusy();
    try {
        const result = await api(
            `/api/uploads?filename=${encodeURIComponent(file.name)}&language=${encodeURIComponent(languageParameter(datasetLanguages()))}${datasetLanguages()
                .map((tag) => `&languages=${encodeURIComponent(tag)}`)
                .join("")}`,
            {
                method: "POST",
                body: file,
                headers: { "Content-Type": "application/octet-stream" },
                signal: controller.signal,
            },
        );
        if (
            sequence !== state.uploadSequence ||
            selectedSource() !== "upload"
        ) {
            await discardUpload(result);
            return;
        }
        state.upload = result;
        const fileLanguage = /\.txt$/i.test(file.name)
            ? `, all ${languageName(datasetLanguages()[0])}`
            : "";
        status.textContent = `${result.filename}: ${Number(result.records).toLocaleString()} texts found${fileLanguage}.`;
        if (!state.samplesEdited) {
            $("samples").value = Math.min(1000, Number(result.records) || 1);
            updateShareCounts();
        }
        schedulePreview();
    } catch (error) {
        if (error.name !== "AbortError" && sequence === state.uploadSequence)
            status.textContent = error.message;
    } finally {
        if (sequence === state.uploadSequence) {
            state.uploadPending = false;
            setBusy();
        }
    }
}

function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewController?.abort();
    state.previewSequence++;
    $("preview-error").hidden = true;
    $("preview-loading").hidden = false;
    $("preview").setAttribute("aria-busy", "true");
    if (!document.hidden) state.previewTimer = setTimeout(loadPreview, 300);
}

function showPreviewError(message) {
    $("preview-loading").hidden = true;
    $("preview-error").textContent = message;
    $("preview-error").hidden = false;
    $("preview-grid").replaceChildren();
    $("preview-text").textContent = "—";
    $("preview").setAttribute("aria-busy", "false");
}

function checkedPreviewImage(image) {
    if (
        typeof image !== "string" ||
        !image.startsWith("data:image/png;base64,")
    )
        throw new Error("The preview returned an unexpected image.");
    return image;
}

// Show a few of the style's backgrounds, rotating as the user asks for more.
function previewBackgrounds(settings) {
    const all = settings.background_presets || [];
    if (all.length <= PREVIEW_EXAMPLES) return all;
    const start = (state.previewIteration * PREVIEW_EXAMPLES) % all.length;
    return Array.from(
        { length: PREVIEW_EXAMPLES },
        (_, index) => all[(start + index) % all.length],
    );
}

function previewTile(image, caption, alt) {
    const figure = document.createElement("figure");
    figure.className = "preview-tile";
    const wrap = document.createElement("div");
    wrap.className = "preview-image";
    const img = document.createElement("img");
    img.src = checkedPreviewImage(image);
    img.alt = alt;
    wrap.append(img);
    const label = document.createElement("figcaption");
    label.textContent = caption;
    figure.append(wrap, label);
    return figure;
}

async function loadPreview() {
    if (!state.info) return;
    state.previewController?.abort();
    const controller = new AbortController();
    state.previewController = controller;
    const sequence = ++state.previewSequence;
    const settings = currentSettings();
    let language = settings.language;
    let text = "";
    const unavailable =
        languageError() ||
        fontError() ||
        (settings.source === "upload" && !state.upload
            ? "Upload a file to preview its text."
            : "");
    if (unavailable) {
        showPreviewError(unavailable);
        return;
    }
    if (settings.source === "upload") {
        text = state.upload.preview_text;
        language = state.upload.preview_language || language;
    }
    const backgrounds = previewBackgrounds(settings);
    const augmented = settings.augmentation > 0 && backgrounds.length > 0;
    $("refresh-preview").disabled = true;
    try {
        const corpus = settings.source === "corpus";
        const result = await post(
            "/api/preview",
            {
                ...appearanceSettings(),
                text,
                language,
                languages: corpus
                    ? settings.languages
                    : [
                          { sorani: "ckb", kurmanji: "kmr" }[language] ||
                              language,
                      ],
                source: corpus ? "corpus" : "demo",
                index: state.previewIteration % 32,
                augmentation: augmented ? 1 : 0,
                background_presets: augmented ? backgrounds : [],
                background_weights: {},
                seed: 42 + state.previewIteration,
                font_size: settings.font_size,
                fonts: settings.fonts,
                compare: false,
            },
            { signal: controller.signal },
        );
        if (sequence !== state.previewSequence) return;
        const grid = $("preview-grid");
        const tiles = [];
        // A style that leaves some images clean shows that clean image too.
        if (!augmented || settings.augmentation < 1)
            tiles.push(
                previewTile(
                    (result.before || result).image,
                    "Clean",
                    `Clean example: ${result.text}`,
                ),
            );
        if (augmented)
            for (const variant of result.variants || [])
                tiles.push(
                    previewTile(
                        variant.image,
                        variant.name,
                        `${variant.name} example: ${result.text}`,
                    ),
                );
        grid.replaceChildren(...tiles);
        grid.dataset.count = String(tiles.length);
        // Long lines become very wide images; give each one the full width.
        const before = result.before || result;
        grid.classList.toggle("wide", before.width > 6 * before.height);
        language = result.source_language || language;
        $("preview-text").textContent = result.text;
        $("preview-text").lang =
            { sorani: "ckb", kurmanji: "kmr" }[language] || language;
        $("preview-loading").hidden = true;
        $("preview").setAttribute("aria-busy", "false");
    } catch (error) {
        if (error.name === "AbortError" || sequence !== state.previewSequence)
            return;
        showPreviewError(
            error.message.includes("synthocr prepare")
                ? "The built-in text is prepared on the first run. You can generate now; the preview appears afterwards."
                : error.message,
        );
    } finally {
        if (sequence === state.previewSequence)
            $("refresh-preview").disabled = false;
    }
}

function fontLabel(font) {
    if (typeof font === "string")
        return font
            .split(/[\\/]/)
            .pop()
            .replace(/#\d+$/, "")
            .replace(/\.[^.]+$/, "")
            .replace(/\[[^\]]*\]/g, "")
            .replace(/[_-]/g, " ")
            .replace(/([a-z])([A-Z])/g, "$1 $2");
    if (font && typeof font === "object")
        return font.family || font.name || "Matched automatically";
    return "Matched automatically";
}

function setBusy() {
    const busy = Boolean(state.runningId);
    const unavailable =
        Boolean(languageError()) ||
        Boolean(fontError()) ||
        state.fontPending ||
        (selectedSource() === "upload" &&
            (state.uploadPending || !state.upload));
    $("generate-button").disabled = busy || !state.info || unavailable;
    $("generate-label").textContent = busy
        ? "Generating…"
        : state.uploadPending
          ? "Uploading…"
          : "Generate dataset";
}

async function submitJob(event) {
    event.preventDefault();
    setError();
    if (languageError() || state.uploadPending || state.runningId) return;
    if (selectedSource() === "upload" && !state.upload) {
        setError("Upload a text file first.");
        return;
    }
    if (!$("generation-form").reportValidity()) return;
    // Ask for the folder first, while the click still counts as a user action.
    if (canChooseFolder && !state.folder && !(await chooseFolder())) {
        setError("Choose a folder to save the dataset in.");
        return;
    }
    $("generate-button").disabled = true;
    $("generate-label").textContent = "Starting…";
    try {
        if (selectedSource() === "upload" && state.upload?.consumed) {
            await uploadSelectedFile();
            if (!state.upload) return;
        }
        // A fresh seed per run, so two runs never repeat the same images. The
        // seed is saved with the dataset, so each run is still reproducible.
        const job = await post("/api/jobs", {
            ...currentSettings(),
            seed: randomSeed(),
        });
        if (selectedSource() === "upload" && state.upload)
            state.upload.consumed = true;
        if (state.folder)
            state.saveTargets.set(job.id, {
                folder: state.folder,
                format: selectedFormat(),
                saved: false,
            });
        state.runningId = job.id;
        state.galleryJob = null;
        showJob(job);
        await refreshJobs();
        state.nameEdited = false;
        suggestName();
        schedulePoll(400);
        $("generation-results").scrollIntoView({
            behavior: "smooth",
            block: "start",
        });
    } catch (error) {
        setError(error.message);
    } finally {
        setBusy();
    }
}

function jobMessage(job) {
    if (job.status === "completed")
        return "Done. Every image and label was checked.";
    if (job.status === "failed") return "Generation stopped.";
    if (job.status === "cancelled") return "Cancelled.";
    if (job.status === "cancelling") return "Stopping…";
    return (
        {
            queued: "Waiting to start…",
            preparing: "Preparing text and fonts…",
            generating: "Making images…",
            verifying: "Checking images and labels…",
        }[job.status] || "Working…"
    );
}

function safeDownload(id, format) {
    return `/api/jobs/${encodeURIComponent(id)}/download?format=${encodeURIComponent(format)}`;
}

async function downloadDataset(format) {
    if (state.downloadPending || state.job?.status !== "completed") return;
    const job = state.job;
    state.downloadPending = true;
    $("download-button").disabled = true;
    $("download-error").hidden = true;
    $("download-status").textContent = "Preparing your download…";
    try {
        const ready = await post(safeDownload(job.id, format), {});
        // Only accept this job's same-origin URL; never buffer a dataset in
        // JavaScript. The browser streams the file and chooses where to save it.
        if (ready.url !== safeDownload(job.id, format))
            throw new Error(
                "The download could not be prepared. Please try again.",
            );
        const link = document.createElement("a");
        link.href = ready.url;
        link.download = ready.filename;
        document.body.append(link);
        link.click();
        link.remove();
        if (state.job?.id === job.id)
            $("download-status").textContent =
                `Download started: ${ready.filename}`;
    } catch (error) {
        if (state.job?.id === job.id) {
            $("download-status").textContent = "";
            $("download-error").textContent = error.message;
            $("download-error").hidden = false;
        }
    } finally {
        state.downloadPending = false;
        $("download-button").disabled = false;
    }
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
});

function crc32(bytes, crc = 0) {
    crc = ~crc >>> 0;
    for (let index = 0; index < bytes.length; index++)
        crc = CRC_TABLE[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
    return ~crc >>> 0;
}

// Reads a byte stream in exact-sized pieces without holding more than one
// network chunk at a time.
function byteReader(stream) {
    const reader = stream.getReader();
    const chunks = [];
    let length = 0;
    async function more() {
        const { done, value } = await reader.read();
        if (done) return false;
        if (value.length) {
            chunks.push(value);
            length += value.length;
        }
        return true;
    }
    function shift(limit) {
        const chunk = chunks[0];
        const size = Math.min(chunk.length, limit);
        const piece = chunk.subarray(0, size);
        if (size === chunk.length) chunks.shift();
        else chunks[0] = chunk.subarray(size);
        length -= size;
        return piece;
    }
    return {
        async take(count) {
            while (length < count)
                if (!(await more()))
                    throw new Error("The dataset download ended early.");
            const result = new Uint8Array(count);
            for (let offset = 0; offset < count; ) {
                const piece = shift(count - offset);
                result.set(piece, offset);
                offset += piece.length;
            }
            return result;
        },
        async *pieces(count) {
            for (let left = count; left > 0; ) {
                if (!length && !(await more()))
                    throw new Error("The dataset download ended early.");
                const piece = shift(left);
                left -= piece.length;
                yield piece;
            }
        },
        cancel: () => reader.cancel(),
    };
}

// Only plain relative names are written; nothing can escape the chosen folder.
function safeArchivePath(name) {
    const parts = name.split("/");
    if (
        !name ||
        name.length > 1024 ||
        /[\\\u0000-\u001f]/.test(name) ||
        parts.some(
            (part) =>
                !part || part === "." || part === ".." || part.length > 255,
        )
    )
        throw new Error(`The dataset contains an unsafe file name: ${name}`);
    return parts;
}

// Write each entry of the server's uncompressed ZIP straight into a folder.
async function extractStoredZip(stream, folder, onFile = () => {}) {
    const reader = byteReader(stream);
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const directories = new Map([["", folder]]);
    let files = 0;
    try {
        for (;;) {
            const signature = new DataView(
                (await reader.take(4)).buffer,
            ).getUint32(0, true);
            if (signature !== 0x04034b50) break; // The central directory follows the files.
            const header = new DataView((await reader.take(26)).buffer);
            const flags = header.getUint16(2, true);
            const method = header.getUint16(4, true);
            const expectedCrc = header.getUint32(10, true);
            let size = header.getUint32(18, true);
            const nameLength = header.getUint16(22, true);
            const extraLength = header.getUint16(24, true);
            const name = decoder.decode(await reader.take(nameLength));
            const extra = new DataView((await reader.take(extraLength)).buffer);
            if (method !== 0 || flags & 0x8)
                throw new Error(
                    "The dataset archive uses an unsupported layout.",
                );
            if (size === 0xffffffff) {
                for (let offset = 0; offset + 4 <= extraLength; ) {
                    const id = extra.getUint16(offset, true);
                    const length = extra.getUint16(offset + 2, true);
                    if (id === 1)
                        size = Number(extra.getBigUint64(offset + 4, true));
                    offset += 4 + length;
                }
            }
            const parts = safeArchivePath(name);
            let path = "";
            let directory = folder;
            for (const part of parts.slice(0, -1)) {
                path += `${part}/`;
                if (!directories.has(path))
                    directories.set(
                        path,
                        await directory.getDirectoryHandle(part, {
                            create: true,
                        }),
                    );
                directory = directories.get(path);
            }
            const handle = await directory.getFileHandle(parts.at(-1), {
                create: true,
            });
            const writable = await handle.createWritable();
            let crc = 0;
            try {
                for await (const piece of reader.pieces(size)) {
                    crc = crc32(piece, crc);
                    await writable.write(piece);
                }
                if (crc !== expectedCrc)
                    throw new Error(`${name} was damaged during transfer.`);
                await writable.close();
            } catch (error) {
                await writable.abort?.();
                throw error;
            }
            onFile(++files, name);
        }
    } finally {
        reader.cancel().catch(() => {});
    }
    return files;
}

// A dataset never overwrites existing files in the chosen folder.
async function newDatasetFolder(parent, name) {
    let folder;
    try {
        folder = await parent.getDirectoryHandle(name);
    } catch (error) {
        if (error.name !== "NotFoundError") throw error;
        return parent.getDirectoryHandle(name, { create: true });
    }
    for await (const _ of folder.keys())
        throw new Error(
            `${parent.name}/${name} already exists and is not empty. Choose another folder.`,
        );
    return folder;
}

async function saveToFolder(job, target) {
    if (state.saving) return;
    state.saving = true;
    $("save-button").disabled = true;
    $("save-error").hidden = true;
    const where = `${target.folder.name}/${job.name}`;
    $("save-status").textContent = `Preparing files for ${where}…`;
    let written = 0;
    try {
        const ready = await post(safeDownload(job.id, target.format), {});
        if (ready.url !== safeDownload(job.id, target.format))
            throw new Error(
                "The files could not be prepared. Please try again.",
            );
        const response = await fetch(ready.url);
        if (!response.ok) throw new Error(await errorMessage(response));
        const folder = await newDatasetFolder(target.folder, job.name);
        const files = await extractStoredZip(response.body, folder, (count) => {
            written = count;
            if (count % 25 === 0)
                $("save-status").textContent =
                    `Saving to ${where}… ${count.toLocaleString()} files`;
        });
        target.saved = true;
        $("save-status").textContent =
            `Saved ${files.toLocaleString()} files to ${where}`;
        $("save-label").textContent = "Save to another folder…";
        toast(`Saved to ${where}`);
    } catch (error) {
        $("save-status").textContent = "";
        $("save-error").textContent = written
            ? `${error.message} The ${written.toLocaleString()} files already saved in ${where} were kept.`
            : error.message;
        $("save-error").hidden = false;
    } finally {
        state.saving = false;
        $("save-button").disabled = false;
    }
}

async function saveCurrentJob() {
    if (state.job?.status !== "completed" || state.saving) return;
    if (!(await chooseFolder())) return;
    const target = {
        folder: state.folder,
        format: state.saveTargets.get(state.job.id)?.format || selectedFormat(),
        saved: false,
    };
    state.saveTargets.set(state.job.id, target);
    await saveToFolder(state.job, target);
}

function showJob(job) {
    $("generation-results").hidden = false;
    const previous = state.job;
    state.job = job;
    $("job-name").textContent = job.name || "";
    $("job-status").textContent =
        {
            completed: "Ready",
            failed: "Failed",
            cancelled: "Cancelled",
            cancelling: "Stopping",
        }[job.status] || "Working";
    $("job-status").className = `status-badge ${job.status}`;
    const progress = Math.min(100, Math.max(0, Number(job.progress) || 0));
    $("job-progress").style.width = `${progress}%`;
    $("job-progress").classList.toggle(
        "indeterminate",
        !terminal.has(job.status) && progress === 0,
    );
    $("job-message").textContent = jobMessage(job);
    $("job-count").textContent =
        `${Number(job.generated || 0).toLocaleString()} / ${Number(job.requested || 0).toLocaleString()} images`;
    $("cancel-job").hidden = terminal.has(job.status);
    $("cancel-job").disabled = job.status === "cancelling";
    $("job-error").hidden = !job.error;
    $("job-error").textContent =
        typeof job.error === "string"
            ? job.error
            : job.error
              ? JSON.stringify(job.error)
              : "";
    const completed = job.status === "completed";
    const target = state.saveTargets.get(job.id);
    if (previous?.id !== job.id) {
        $("download-status").textContent = "";
        $("download-error").hidden = true;
        $("save-error").hidden = true;
        $("save-status").textContent = target?.saved
            ? `Saved to ${target.folder.name}/${job.name}`
            : canChooseFolder
              ? "Choose a folder to save this dataset."
              : "Download the dataset as a ZIP.";
        $("save-label").textContent = target?.saved
            ? "Save to another folder…"
            : "Save to a folder…";
    }
    $("save-panel").hidden = !completed;
    $("save-button").hidden = !canChooseFolder;
    $("gallery-section").hidden = !completed;
    if (completed && state.galleryJob !== job.id) loadGallery(job.id);
    if (!terminal.has(job.status)) state.runningId = job.id;
    else if (state.runningId === job.id) state.runningId = null;
    setBusy();
    if (completed && target && !target.saved && !state.saving)
        saveToFolder(job, target);
    else if (
        previous?.id === job.id &&
        !terminal.has(previous.status) &&
        completed
    )
        toast("Your dataset is ready.");
}

async function loadGallery(id, offset = 0) {
    state.galleryController?.abort();
    const controller = new AbortController();
    state.galleryController = controller;
    const sequence = ++state.gallerySequence;
    $("gallery-count").textContent = "Loading…";
    $("gallery-previous").disabled = true;
    $("gallery-next").disabled = true;
    try {
        const result = await api(
            `/api/jobs/${encodeURIComponent(id)}/samples?offset=${offset}&limit=${GALLERY_PAGE}`,
            { signal: controller.signal },
        );
        if (sequence !== state.gallerySequence || state.job?.id !== id) return;
        state.galleryJob = id;
        state.galleryOffset = offset;
        const cards = result.samples.map((sample) => {
            const card = document.createElement("figure");
            card.className = "gallery-card";
            const link = document.createElement("a");
            link.className = "gallery-image";
            link.href = sample.image;
            link.target = "_blank";
            link.rel = "noopener";
            link.setAttribute(
                "aria-label",
                `Open image ${sample.id} full size`,
            );
            const img = document.createElement("img");
            img.src = sample.thumbnail || sample.image;
            img.alt = `Image ${sample.id}: ${sample.text}`;
            img.loading = "lazy";
            img.decoding = "async";
            link.append(img);
            const caption = document.createElement("figcaption");
            caption.textContent = sample.text;
            caption.title = sample.text;
            caption.dir = "auto";
            caption.lang = sample.language || "und";
            const meta = document.createElement("small");
            meta.textContent = `${sample.id} · ${fontLabel(sample.font)}`;
            card.append(link, caption, meta);
            return card;
        });
        $("sample-gallery").replaceChildren(...cards);
        const total = Number(result.total || 0);
        $("gallery-count").textContent = total
            ? `${offset + 1}–${offset + result.samples.length} of ${total.toLocaleString()}`
            : "No images";
        $("gallery-previous").disabled = offset === 0;
        $("gallery-next").disabled = offset + result.samples.length >= total;
        $("gallery-pagination").hidden = total <= GALLERY_PAGE;
    } catch (error) {
        if (error.name !== "AbortError" && sequence === state.gallerySequence)
            $("gallery-count").textContent = error.message;
    }
}

function renderRecent() {
    const jobs = state.jobs.filter((job) => job.id !== state.job?.id);
    $("recent-section").hidden = jobs.length === 0;
    const list = $("recent-runs");
    list.replaceChildren();
    for (const job of jobs.slice(0, 5)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "recent-run";
        const created = new Date(job.created_at);
        const title = document.createElement("strong");
        title.textContent = job.name;
        const description = document.createElement("small");
        description.textContent = `${Number(job.generated || 0).toLocaleString()} images · ${selectionName(job.languages, job.language)}${Number.isNaN(created.valueOf()) ? "" : ` · ${created.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`}`;
        const text = document.createElement("span");
        text.append(title, description);
        const status = document.createElement("span");
        status.className = `status-badge ${job.status}`;
        status.textContent = job.status === "completed" ? "Ready" : job.status;
        button.append(text, status);
        button.addEventListener("click", async () => {
            try {
                showJob(await api(`/api/jobs/${encodeURIComponent(job.id)}`));
                renderRecent();
                $("generation-results").scrollIntoView({
                    behavior: "smooth",
                    block: "start",
                });
                schedulePoll();
            } catch (error) {
                toast(error.message);
            }
        });
        list.append(button);
    }
}

async function refreshJobs() {
    const response = await api("/api/jobs");
    state.jobs = response.jobs;
    const running = state.jobs.find((job) => !terminal.has(job.status));
    state.runningId = running?.id || null;
    setBusy();
    renderRecent();
    return running;
}

function schedulePoll(delay = 1200) {
    clearTimeout(state.pollTimer);
    if (state.runningId || (state.job && !terminal.has(state.job.status)))
        state.pollTimer = setTimeout(pollJob, document.hidden ? 10000 : delay);
}

async function pollJob() {
    try {
        const id = state.runningId || state.job?.id;
        if (!id) return;
        const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
        if (state.job?.id === id) showJob(job);
        else if (terminal.has(job.status)) {
            state.runningId = null;
            setBusy();
        }
        state.jobs = state.jobs.map((entry) =>
            entry.id === job.id ? job : entry,
        );
        renderRecent();
    } catch (error) {
        $("job-message").textContent =
            `Connection lost. Retrying… ${error.message}`;
    } finally {
        schedulePoll();
    }
}

async function cancelJob() {
    if (!state.job || terminal.has(state.job.status)) return;
    $("cancel-job").disabled = true;
    try {
        showJob(
            await post(
                `/api/jobs/${encodeURIComponent(state.job.id)}/cancel`,
                {},
            ),
        );
        schedulePoll(500);
    } catch (error) {
        toast(error.message);
        $("cancel-job").disabled = false;
    }
}

async function initialize() {
    try {
        state.info = await api("/api/info");
        $("app-version").textContent = `v${state.info.version}`;
        for (const [name, preset] of Object.entries(
            state.info.image_presets || {},
        )) {
            const label = document.querySelector(
                `[data-style-label="${name}"]`,
            );
            const description = document.querySelector(
                `[data-style-description="${name}"]`,
            );
            if (label) label.textContent = preset.label;
            if (description) description.textContent = preset.description;
        }
        mountBackgroundOptions(state.info.document_presets || {});
        mountEffectOptions(state.info.document_layers || []);
        $("text-size").value = String(state.info.defaults?.font_size || 48);
        $("text-size-value").textContent = `${$("text-size").value} px`;
        await loadFonts();
        showStyleControls();
        updateSource();
        loadPreview();
        const running = await refreshJobs();
        suggestName();
        if (running) {
            showJob(running);
            schedulePoll();
        }
    } catch (error) {
        showPreviewError(error.message);
        setError(
            "SynthOCR could not be reached. Make sure the app is running, then refresh.",
        );
    }
}

$("theme-toggle").addEventListener("click", () => {
    themePreference.set(
        document.documentElement.dataset.theme === "dark" ? "light" : "dark",
    );
    updateThemeButton();
});
$("generation-form").addEventListener("submit", submitJob);
document
    .querySelectorAll('input[name="source"]')
    .forEach((input) => input.addEventListener("change", updateSource));
document.querySelectorAll('input[name="language"]').forEach((input) =>
    input.addEventListener("change", () => {
        updateSource();
        if (selectedSource() === "upload" && $("corpus-file").files.length)
            uploadSelectedFile();
    }),
);
document.querySelectorAll('input[name="style"]').forEach((input) =>
    input.addEventListener("change", () => {
        state.style = input.value;
        state.custom = null;
        showStyleControls();
        schedulePreview();
    }),
);
$("effects-amount").addEventListener("input", () => {
    $("effects-amount-value").textContent = `${$("effects-amount").value}%`;
    readCustomControls();
});
$("background-options").addEventListener("change", readCustomControls);
$("effect-options").addEventListener("change", readCustomControls);
for (const name of Object.keys(GEOMETRY))
    $(GEOMETRY[name].id).addEventListener("input", readCustomControls);
$("text-size").addEventListener("input", () => {
    $("text-size-value").textContent = `${$("text-size").value} px`;
    schedulePreview();
});
$("font-file").addEventListener("change", addFonts);
$("font-list").addEventListener("change", (event) => {
    const choice = new Set(
        state.fontChoice || state.fontCatalog.map((font) => font.id),
    );
    if (event.target.checked) choice.add(event.target.value);
    else choice.delete(event.target.value);
    setFontChoice(choice);
});
$("fonts-all").addEventListener("click", () => setFontChoice(null));
$("fonts-none").addEventListener("click", () => setFontChoice(new Set()));
$("fonts-languages").addEventListener("click", () => {
    const tags = datasetLanguages();
    setFontChoice(
        new Set(
            state.fontCatalog
                .filter((font) =>
                    tags.some((tag) => font.languages.includes(tag)),
                )
                .map((font) => font.id),
        ),
    );
});
$("corpus-file").addEventListener("change", () => {
    updateSource();
    uploadSelectedFile();
});
$("file-language-options").addEventListener("change", (event) => {
    state.fileLanguage = event.target.value;
    updateSource();
    uploadSelectedFile();
});
$("dataset-name").addEventListener("input", () => {
    state.nameEdited = true;
    updateFolder();
});
$("samples").addEventListener("input", () => {
    state.samplesEdited = true;
    updateShareCounts();
});
$("choose-folder").addEventListener("click", chooseFolder);
$("refresh-preview").addEventListener("click", () => {
    state.previewIteration = (state.previewIteration + 1) % 32;
    loadPreview();
});
$("cancel-job").addEventListener("click", cancelJob);
$("save-button").addEventListener("click", saveCurrentJob);
// Downloads go straight through the browser; never buffer a dataset in JavaScript.
$("download-button").addEventListener("click", () =>
    downloadDataset(
        state.saveTargets.get(state.job?.id)?.format || selectedFormat(),
    ),
);
$("gallery-previous").addEventListener("click", () =>
    loadGallery(state.job.id, Math.max(0, state.galleryOffset - GALLERY_PAGE)),
);
$("gallery-next").addEventListener("click", () =>
    loadGallery(state.job.id, state.galleryOffset + GALLERY_PAGE),
);
$("generate-button").disabled = true;
document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
        clearTimeout(state.previewTimer);
        state.previewController?.abort();
        state.galleryController?.abort();
    } else {
        schedulePreview();
        if (
            state.job?.status === "completed" &&
            state.galleryJob !== state.job.id
        )
            loadGallery(state.job.id, state.galleryOffset);
    }
    schedulePoll(document.hidden ? 10000 : 100);
});
updateThemeButton();
updateFolder();
initialize();
