import io
import re
from PIL import Image

try:
    from pix2tex.cli import LatexOCR
    import latex2mathml.converter
    HAS_PIX2TEX = True
except ImportError:
    HAS_PIX2TEX = False

# Initialize model once
model = None

def get_mathml_from_image(img_bytes):
    global model
    if not HAS_PIX2TEX:
        return '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mtext>Math extraction requires pix2tex and latex2mathml</mml:mtext></mml:math>'
    
    if model is None:
        try:
            model = LatexOCR()
        except Exception as e:
            err_msg = str(e).replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            return f'<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mtext>Error loading OCR model: {err_msg}</mml:mtext></mml:math>'
            
    try:
        img = Image.open(io.BytesIO(img_bytes))
        latex = model(img)
        
        # Clean latex string if needed
        if not isinstance(latex, str):
            latex = str(latex)
            
        mathml = latex2mathml.converter.convert(latex)
        # Clean up the mathml to remove the xml declaration and just return the math element
        mathml = mathml.replace('<?xml version="1.0" encoding="UTF-8"?>', '').strip()
        
        # Pix2tex might return empty or error, handle basic cases
        if not mathml.startswith('<math'):
            safe_latex = latex.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            mathml = f'<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mtext>{safe_latex}</mml:mtext></mml:math>'
        else:
            # Replace default namespace with mml prefix namespace declaration
            mathml = mathml.replace('xmlns="http://www.w3.org/1998/Math/MathML"', 'xmlns:mml="http://www.w3.org/1998/Math/MathML"')
            # Namespace replacement using regex for all MathML tags (they all start with 'm')
            mathml = re.sub(r'<(/?)(m[a-z]+)', r'<\1mml:\2', mathml)
            
        return mathml
    except Exception as e:
        err_msg = str(e).replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
        return f'<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mtext>Math extraction failed: {err_msg}</mml:mtext></mml:math>'
