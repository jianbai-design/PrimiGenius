import purify from 'dompurify';

// Keep SVG references local to the sanitized document, including Cairo glyphs.
function sanitizeSvg(text) {
    try {
        const fragment = purify.sanitize(text, {
            USE_PROFILES: {svg: true, svgFilters: true}, ADD_TAGS: ['use'],
            FORBID_ATTR: ['xml:base'], RETURN_DOM_FRAGMENT: true
        });
        const svg = fragment.querySelector('svg');
        if (!svg) return '';
        const ids = new Map();
        for (const element of [svg, ...svg.querySelectorAll('[id]')]) {
            const id = element.getAttribute('id');
            if (id) ids.set(id, ids.has(id) ? null : element);
        }
        const references = new Map();
        for (const use of svg.querySelectorAll('use')) {
            const href = (use.getAttribute('href') ?? use.getAttributeNS('http://www.w3.org/1999/xlink', 'href') ?? '').trim();
            const target = /^#[^\s#]+$/.test(href) ? ids.get(href.slice(1)) : null;
            if (!target || target.namespaceURI !== 'http://www.w3.org/2000/svg' || target.contains(use) ||
                !/^(path|g|symbol|svg|use|rect|circle|ellipse|line|polyline|polygon|text|tspan|image)$/.test(target.localName)) {
                use.remove();
                continue;
            }
            // SVG2 href has precedence. Remove the legacy attribute so an
            // alternate renderer cannot follow a different (external) URL.
            use.removeAttributeNS('http://www.w3.org/1999/xlink', 'href');
            use.removeAttribute('xlink:href');
            use.setAttribute('href', href);
            references.set(use, target);
        }
        const visiting = new Set(), costs = new Map();
        const cost = (target, depth = 0) => {
            if (depth > 32 || visiting.has(target)) return Infinity;
            if (costs.has(target)) return costs.get(target);
            visiting.add(target);
            let total = target.querySelectorAll('*').length + 1;
            for (const use of target.localName === 'use' ? [target] : target.querySelectorAll('use')) {
                if (references.has(use)) total += cost(references.get(use), depth + 1);
                if (total > 100000) break;
            }
            visiting.delete(target);
            costs.set(target, total);
            return total;
        };
        let expanded = 0;
        for (const [use, target] of references) {
            const count = cost(target);
            if (expanded + count > 100000) use.remove();
            else expanded += count;
        }
        return new XMLSerializer().serializeToString(svg);
    } finally { purify.removed.length = 0; }
}

export default {
    sanitizeSvg,
    sanitize(text,options){
        try{return purify.sanitize(text,options);}
        // DOMPurify's diagnostic array can retain removed SVG attributes and their documents.
        // The application only needs the sanitized result.
        finally{purify.removed.length=0;}
    }
};
