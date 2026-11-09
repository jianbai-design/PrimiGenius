const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const https = require("https");
const path = require("path");
const { publicGithubCandidates } = require("../src/github-public");

const PROJECT_DIR = path.resolve(__dirname, "..");
const TOOL_VERSION = "2.6.0";
const TOOL_ARCHIVE_NAME = `winCodeSign-${TOOL_VERSION}.7z`;
const TOOL_URL = `https://github.com/electron-userland/electron-builder-binaries/releases/download/winCodeSign-${TOOL_VERSION}/${TOOL_ARCHIVE_NAME}`;
const TOOL_DIR = path.join(__dirname, "winCodeSign");
const TOOL_ARCHIVE_PATH = path.join(__dirname, TOOL_ARCHIVE_NAME);
const CACHE_ROOT = process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, "electron-builder", "Cache", "winCodeSign")
    : null;

function exists(targetPath) {
    try {
        fs.accessSync(targetPath);
        return true;
    } catch {
        return false;
    }
}

function isToolReady(toolDir) {
    return exists(path.join(toolDir, "rcedit-x64.exe"));
}

function ensureDirectory(dirPath) {
    fs.mkdirSync(dirPath, { recursive: true });
}

function repairSymlinkPlaceholders(toolDir) {
    const dylibPairs = [
        ["darwin/10.12/lib/libcrypto.dylib", "darwin/10.12/lib/libcrypto.1.0.0.dylib"],
        ["darwin/10.12/lib/libssl.dylib", "darwin/10.12/lib/libssl.1.0.0.dylib"],
    ];

    for (const [linkRelativePath, sourceRelativePath] of dylibPairs) {
        const linkPath = path.join(toolDir, ...linkRelativePath.split("/"));
        const sourcePath = path.join(toolDir, ...sourceRelativePath.split("/"));

        if (!exists(linkPath) || !exists(sourcePath)) {
            continue;
        }

        const stats = fs.statSync(linkPath);
        if (stats.size === 0) {
            fs.copyFileSync(sourcePath, linkPath);
        }
    }
}

function copyDirectory(sourceDir, targetDir) {
    fs.rmSync(targetDir, { recursive: true, force: true });
    fs.cpSync(sourceDir, targetDir, { recursive: true });
    repairSymlinkPlaceholders(targetDir);
}

function findCachedToolDirectory() {
    if (!CACHE_ROOT || !exists(CACHE_ROOT)) {
        return null;
    }

    const candidates = fs.readdirSync(CACHE_ROOT, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => path.join(CACHE_ROOT, entry.name))
        .filter((dirPath) => exists(path.join(dirPath, "rcedit-x64.exe")))
        .sort((left, right) => fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs);

    return candidates[0] || null;
}

function findCachedArchive() {
    if (!CACHE_ROOT || !exists(CACHE_ROOT)) {
        return null;
    }

    const archives = fs.readdirSync(CACHE_ROOT, { withFileTypes: true })
        .filter((entry) => entry.isFile() && entry.name.endsWith(".7z"))
        .map((entry) => path.join(CACHE_ROOT, entry.name))
        .sort((left, right) => fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs);

    return archives[0] || null;
}

function downloadFile(url, destinationPath) {
    return new Promise((resolve, reject) => {
        ensureDirectory(path.dirname(destinationPath));

        const request = https.get(url, {
            headers: {
                "User-Agent": "PrimiGenius-Build/1.0",
            },
        }, (response) => {
            clearTimeout(connectionTimeout);
            if (response.statusCode && response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
                response.resume();
                downloadFile(response.headers.location, destinationPath).then(resolve, reject);
                return;
            }

            if (response.statusCode !== 200) {
                response.resume();
                reject(new Error(`Failed to download ${url}: HTTP ${response.statusCode}`));
                return;
            }

            const fileStream = fs.createWriteStream(destinationPath);
            response.pipe(fileStream);

            fileStream.on("finish", () => {
                fileStream.close(resolve);
            });

            fileStream.on("error", (error) => {
                fs.rmSync(destinationPath, { force: true });
                reject(error);
            });
        });

        const connectionTimeout = setTimeout(() => request.destroy(new Error('Build tool connection timed out')), 10000);
        request.on("error", (error) => {
            clearTimeout(connectionTimeout);
            fs.rmSync(destinationPath, { force: true });
            reject(error);
        });
    });
}

async function downloadToolArchive() {
    let mode = 'auto';
    try { mode = JSON.parse(fs.readFileSync(path.join(PROJECT_DIR, '.primigenius_network_profile.json'), 'utf8')).mode; } catch (_) {}
    let lastError;
    for (const url of publicGithubCandidates(TOOL_URL, {}, mode)) {
        try {
            await downloadFile(url, TOOL_ARCHIVE_PATH);
            return;
        } catch (error) { lastError = error; }
    }
    throw lastError;
}

function extractArchive(archivePath, targetDir) {
    const sevenZipPath = require("7zip-bin").path7za;

    fs.rmSync(targetDir, { recursive: true, force: true });
    ensureDirectory(targetDir);

    const result = spawnSync(
        sevenZipPath,
        ["x", "-bd", "-y", archivePath, `-o${targetDir}`],
        {
            stdio: "inherit",
            cwd: PROJECT_DIR,
        }
    );

    if (result.status !== 0 && result.status !== 2) {
        throw new Error(`Failed to extract ${path.basename(archivePath)} (7-Zip exit code ${result.status ?? "unknown"})`);
    }

    repairSymlinkPlaceholders(targetDir);

    if (!isToolReady(targetDir)) {
        throw new Error(`winCodeSign extraction did not produce rcedit-x64.exe in ${targetDir}`);
    }
}

async function ensureLocalTooling() {
    if (isToolReady(TOOL_DIR)) {
        repairSymlinkPlaceholders(TOOL_DIR);
        return TOOL_DIR;
    }

    const cachedToolDir = findCachedToolDirectory();
    if (cachedToolDir) {
        copyDirectory(cachedToolDir, TOOL_DIR);
        if (isToolReady(TOOL_DIR)) {
            return TOOL_DIR;
        }
    }

    if (!exists(TOOL_ARCHIVE_PATH)) {
        const cachedArchive = findCachedArchive();
        if (cachedArchive) {
            fs.copyFileSync(cachedArchive, TOOL_ARCHIVE_PATH);
        } else {
            console.log(`[build] Downloading ${TOOL_ARCHIVE_NAME}...`);
            await downloadToolArchive();
        }
    }

    extractArchive(TOOL_ARCHIVE_PATH, TOOL_DIR);
    return TOOL_DIR;
}

function runLocalRcedit(appBuilderArgs, toolDir) {
    const argsIndex = appBuilderArgs.indexOf("--args");
    if (argsIndex === -1 || argsIndex === appBuilderArgs.length - 1) {
        return Promise.reject(new Error("electron-builder invoked rcedit without --args payload"));
    }

    const rceditArgs = JSON.parse(appBuilderArgs[argsIndex + 1]);
    const rceditBinary = path.join(toolDir, process.arch === "ia32" ? "rcedit-ia32.exe" : "rcedit-x64.exe");

    return new Promise((resolve, reject) => {
        const child = spawn(rceditBinary, rceditArgs, {
            cwd: PROJECT_DIR,
            stdio: "inherit",
        });

        child.on("error", reject);
        child.on("close", (code) => {
            if (code === 0) {
                resolve("");
                return;
            }

            reject(new Error(`rcedit exited with code ${code}`));
        });
    });
}

function patchElectronBuilder(toolDir) {
    const builderUtil = require("builder-util");
    const builderUtilInternal = require("builder-util/out/util");
    const originalExecuteAppBuilder = builderUtilInternal.executeAppBuilder;

    const patchedExecuteAppBuilder = async (args, childProcessConsumer, extraOptions = {}, maxRetries = 0) => {
        if (Array.isArray(args) && args[0] === "rcedit") {
            return runLocalRcedit(args, toolDir);
        }

        return originalExecuteAppBuilder(args, childProcessConsumer, extraOptions, maxRetries);
    };

    builderUtil.executeAppBuilder = patchedExecuteAppBuilder;
    builderUtilInternal.executeAppBuilder = patchedExecuteAppBuilder;
}

async function main() {
    process.chdir(PROJECT_DIR);
    const templateArg = process.argv.indexOf("--pg-nsis-templates");
    if (templateArg === -1) throw new Error("Build through build_installer.bat: protected NSIS templates are required");
    if (templateArg !== -1) {
        const templateRoot = path.resolve(process.argv[templateArg + 1] || "");
        const relative = path.relative(path.join(PROJECT_DIR, "build"), templateRoot);
        if (!relative.startsWith("installer-work-") || relative.includes("..") ||
            !fs.existsSync(path.join(templateRoot, "include", "installUtil.nsh"))) {
            throw new Error("Invalid isolated NSIS template directory");
        }
        const installUtil = fs.readFileSync(path.join(templateRoot, "include", "installUtil.nsh"), "utf8");
        if (!installUtil.includes("!insertmacro PGProtectLegacyData") ||
            installUtil.includes("ExecWait") || installUtil.includes("old-uninstaller.exe")) {
            throw new Error("Unsafe NSIS templates: legacy uninstallation must be removed");
        }
        // Keep electron-builder's normal two-stage signed uninstaller generation.
        require("app-builder-lib/out/targets/nsis/nsisUtil").nsisTemplatesDir = templateRoot;
        process.argv.splice(templateArg, 2);
    }

    const toolDir = await ensureLocalTooling();
    console.log(`[build] Using local rcedit from ${toolDir}`);
    patchElectronBuilder(toolDir);
    require("electron-builder/out/cli/cli");
}

main().catch((error) => {
    console.error(`[build] Failed to prepare electron-builder wrapper: ${error.stack || error}`);
    process.exit(1);
});
