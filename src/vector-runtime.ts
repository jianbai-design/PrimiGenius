import { Buffer } from 'node:buffer';

type Patch = { start: number; removed: Uint16Array; inserted: Uint16Array };
function units(text: string, start: number, end: number): Uint16Array {
    const result = new Uint16Array(end - start);
    for (let index = start; index < end; index++) result[index - start] = text.charCodeAt(index);
    return result;
}
function textOf(value: Uint16Array): string {
    let result = '';
    for (let index = 0; index < value.length; index += 8192) {
        result += String.fromCharCode(...value.subarray(index, index + 8192));
    }
    return result;
}

/** Keep one full document and changes, without slices retaining old documents. */
export class TextHistory {
    private current: string | undefined;
    private past: Patch[] = [];
    private future: Patch[] = [];
    constructor(private limit = 30) {}
    commit(next: string): void {
        if (next === this.current) return;
        if (this.current !== undefined) {
            const previous = this.current;
            let start = 0, oldEnd = previous.length, newEnd = next.length;
            while (start < oldEnd && start < newEnd && previous.charCodeAt(start) === next.charCodeAt(start)) start++;
            while (oldEnd > start && newEnd > start && previous.charCodeAt(oldEnd - 1) === next.charCodeAt(newEnd - 1)) { oldEnd--; newEnd--; }
            this.past.push({start, removed: units(previous, start, oldEnd), inserted: units(next, start, newEnd)});
            if (this.past.length >= this.limit) this.past.shift();
        }
        this.current = next;
        this.future = [];
    }
    private apply(patch: Patch, reverse: boolean): string {
        const removed = reverse ? patch.inserted : patch.removed;
        const inserted = reverse ? patch.removed : patch.inserted;
        const current = this.current!;
        // Flatten the result: a sliced/rope string could keep every old SVG alive.
        this.current = Buffer.from(current.slice(0, patch.start) + textOf(inserted) +
            current.slice(patch.start + removed.length), 'utf16le').toString('utf16le');
        return this.current;
    }
    undo(): string | null {
        const patch = this.past.pop();
        if (!patch) return null;
        this.future.push(patch);
        return this.apply(patch, true);
    }
    redo(): string | null {
        const patch = this.future.pop();
        if (!patch) return null;
        this.past.push(patch);
        return this.apply(patch, false);
    }
    reset(): void { this.current = undefined; this.past = []; this.future = []; }
    get retainedBytes(): number {
        return (this.current?.length || 0) * 2 + [...this.past, ...this.future].reduce((sum, p) => sum + p.removed.byteLength + p.inserted.byteLength, 0);
    }
}

export function latestFrame<T>(callback: (value: T) => void, request = requestAnimationFrame, cancel = cancelAnimationFrame) {
    let handle: number | null = null, latest: T;
    const run = () => { handle = null; callback(latest); };
    return {
        schedule(value: T) { latest = value; if (handle === null) handle = request(run); },
        flush() { if (handle !== null) { cancel(handle); run(); } },
        cancel() { if (handle !== null) cancel(handle); handle = null; }
    };
}

type Bounds = { x: number; y: number; w: number; h: number };
type Item = Bounds & { element: SVGTextElement; order: number };
type Branch = Bounds & { items?: Item[]; left?: Branch; right?: Branch };
const intersects = (a: Bounds, b: Bounds) => a.x <= b.x + b.w && a.x + a.w >= b.x && a.y <= b.y + b.h && a.y + a.h >= b.y;
function tree(items: Item[]): Branch {
    let x = Infinity, y = Infinity, right = -Infinity, bottom = -Infinity;
    for (const item of items) { x = Math.min(x, item.x); y = Math.min(y, item.y); right = Math.max(right, item.x + item.w); bottom = Math.max(bottom, item.y + item.h); }
    const result: Branch = {x, y, w: right - x, h: bottom - y};
    if (items.length <= 8) result.items = items;
    else {
        items.sort(result.w >= result.h ? (a,b) => a.x + a.w/2 - b.x - b.w/2 : (a,b) => a.y + a.h/2 - b.y - b.h/2);
        const middle = items.length >> 1;
        result.left = tree(items.slice(0, middle)); result.right = tree(items.slice(middle));
    }
    return result;
}

/** SVG remains authoritative. This index only narrows the existing exact hit test. */
export class SvgTextIndex {
    private dirty = true;
    private observer: MutationObserver;
    private root: Branch | null = null;
    private fallback: Item[] = [];
    private small: SVGTextElement[] = [];
    private fontsChanged = () => { this.dirty = true; };
    constructor(private svg: SVGSVGElement, private bounds: (element: SVGTextElement) => Bounds | null) {
        this.observer = new MutationObserver(records => this.invalidate(records));
        this.observer.observe(svg, {subtree:true, childList:true, attributes:true, characterData:true});
        document.fonts?.addEventListener('loadingdone', this.fontsChanged);
    }
    private invalidate(records: MutationRecord[]) {
        if (records.some(record => record.attributeName !== 'data-ve-selected')) this.dirty = true;
    }
    private rebuild() {
        const texts = Array.from(this.svg.querySelectorAll<SVGTextElement>('text'));
        this.small = texts;
        this.root = null; this.fallback = [];
        if (texts.length >= 128) {
            const items: Item[] = [];
            texts.forEach((element, order) => {
                if (element.closest('defs,clipPath,mask')) return;
                const box = this.bounds(element);
                if (box && Object.values(box).every(Number.isFinite)) items.push({...box,element,order});
                else this.fallback.push({x:0,y:0,w:0,h:0,element,order});
            });
            if (items.length) this.root = tree(items);
        }
        this.dirty = false;
    }
    query(clientX: number, clientY: number, tolerance: number): SVGTextElement[] {
        this.invalidate(this.observer.takeRecords());
        if (this.dirty) this.rebuild();
        const matrix = this.svg.getScreenCTM();
        if (!this.root || !matrix) return this.small.slice().reverse();
        let inverse: DOMMatrix;
        try { inverse = matrix.inverse(); } catch { return this.small.slice().reverse(); }
        const x = inverse.a * clientX + inverse.c * clientY + inverse.e;
        const y = inverse.b * clientX + inverse.d * clientY + inverse.f;
        // Conservative expansion covers the old local-axis tolerance even under shear.
        const rx = Math.max(0,tolerance) * 2 * (Math.abs(inverse.a) + Math.abs(inverse.c));
        const ry = Math.max(0,tolerance) * 2 * (Math.abs(inverse.b) + Math.abs(inverse.d));
        const area = {x:x-rx,y:y-ry,w:rx*2,h:ry*2};
        const found = this.fallback.slice();
        const visit = (branch: Branch) => {
            if (!intersects(branch, area)) return;
            if (branch.items) { for (const item of branch.items) if (intersects(item, area)) found.push(item); }
            else { visit(branch.left!); visit(branch.right!); }
        };
        visit(this.root);
        return found.sort((a,b) => b.order-a.order).map(item => item.element);
    }
    dispose() { this.observer.disconnect(); document.fonts?.removeEventListener('loadingdone', this.fontsChanged); this.root = null; this.small = []; this.fallback = []; }
}
