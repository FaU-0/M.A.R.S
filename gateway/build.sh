#!/bin/bash
# Собирает gateway/mars.py из исходников в src/
cd "$(dirname "$0")/src"
{ cat a_server.py; printf '\n\nINDEX_HTML = r'"'''"; cat b_head.html c_js1.js c_js2.js c_js3.js d_face1.js d_face2.js d_face3.js e_speech.js f_transcript.js; printf '\nApp.init();\n</script></body></html>'"'''"'\n\n\nif __name__ == "__main__":\n    main()\n'; } > ../mars.py
cd ..
python3 -m py_compile mars.py && echo PYOK
python3 - <<'PY'
import re
s=open('mars.py',encoding='utf-8').read()
i=s.index("INDEX_HTML = r'''")+len("INDEX_HTML = r'''"); j=s.index("'''\n\n\nif __name__")
m=re.search(r'<script>(.*)</script>',s[i:j],re.S); open('/tmp/mars_client_check.js','w',encoding='utf-8').write(m.group(1))
PY
node --check /tmp/mars_client_check.js && echo JSOK
