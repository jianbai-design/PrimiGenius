'use strict';
// Baseline TIFF 6.0: one uncompressed RGBA strip, unassociated alpha, physical DPI.
// Kept separate so encoding runs in a disposable worker without loading a viewer.
(function(root) {
    function tiffSize(width,height) { return width*height*4+218; }
    function tiff(rgba, width, height, dpi = 300) {
        if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
            width * height > 32_000_000 || rgba.byteLength !== width * height * 4 || !Number.isFinite(dpi) || dpi <= 0) {
            throw Error('Invalid or oversized TIFF image');
        }
        const count = 15, extra = 8 + 2 + count * 12 + 4, pixels = extra + 24;
        const buffer = new ArrayBuffer(tiffSize(width,height)), view = new DataView(buffer);
        view.setUint16(0, 0x4949, true); view.setUint16(2, 42, true); view.setUint32(4, 8, true);
        view.setUint16(8, count, true);
        let offset = 10;
        const tag = (id, type, n, value) => {
            view.setUint16(offset, id, true); view.setUint16(offset+2, type, true);
            view.setUint32(offset+4, n, true); view.setUint32(offset+8, value, true); offset += 12;
        };
        tag(256,4,1,width); tag(257,4,1,height); tag(258,3,4,extra); tag(259,3,1,1);
        tag(262,3,1,2); tag(273,4,1,pixels); tag(274,3,1,1); tag(277,3,1,4);
        tag(278,4,1,height); tag(279,4,1,rgba.byteLength); tag(282,5,1,extra+8);
        tag(283,5,1,extra+16); tag(284,3,1,1); tag(296,3,1,2); tag(338,3,1,2);
        for (let i=0; i<4; i++) view.setUint16(extra+i*2, 8, true);
        for (const at of [extra+8,extra+16]) {
            view.setUint32(at, Math.round(dpi*1000), true); view.setUint32(at+4, 1000, true);
        }
        new Uint8Array(buffer,pixels).set(new Uint8Array(rgba));
        return buffer;
    }
    const units = {px:1,pt:96/72,mm:96/25.4};
    const api = {tiff, tiffSize, units};
    if (typeof module === 'object') module.exports = api;
    else root.VectorCodecs = api;
})(typeof self === 'object' ? self : globalThis);
