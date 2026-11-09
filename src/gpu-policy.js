'use strict';

// Leave Chromium's driver blocklist and automatic software fallback intact.
// Software mode is an escape hatch for remote desktops and broken drivers.
function configureGraphics(app, environment = process.env) {
    const mode = environment.PRIMIGENIUS_GPU_MODE === 'software' ? 'software' : 'auto';
    if (mode === 'software') app.disableHardwareAcceleration();
    let features = {}, gpuFailures = 0;
    const update = () => {
        try { features = {...app.getGPUFeatureStatus()}; } catch (_) {}
    };
    app.on('gpu-info-update', update);
    app.on('child-process-gone', (_event, details) => { if (details.type === 'GPU') gpuFailures++; });
    return {
        snapshot() {
            if (app.isReady()) update();
            return {mode, features:{...features}, gpuFailures};
        }
    };
}

module.exports = {configureGraphics};
