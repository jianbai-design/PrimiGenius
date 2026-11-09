import purify from 'dompurify';
export default {
    sanitize(text,options){
        try{return purify.sanitize(text,options);}
        // DOMPurify's diagnostic array can retain removed SVG attributes and their documents.
        // The application only needs the sanitized result.
        finally{purify.removed.length=0;}
    }
};
