"""瀏覽器 DOM 互動元素快照 JS 建構模組。"""

from mcp_servers._shared import CONSTANTS


def build_snapshot_js(max_label_chars: int = CONSTANTS.browser_snapshot_max_label_chars) -> str:
    """產生清除舊標記、偵測敏感欄位並附帶狀態資訊的 DOM 快照 JavaScript。"""
    limit = max(20, int(max_label_chars))
    return f"""
(selector) => {{
  for (const old of document.querySelectorAll('[data-mcp-ref]')) {{
    old.removeAttribute('data-mcp-ref');
  }}
  const root = selector ? document.querySelector(selector) : document;
  if (!root) {{
    return {{ found: false, text: '' }};
  }}
  const sels = 'a[href], button, input, textarea, select, summary, ' +
    '[role="button"], [role="link"], [role="tab"], [role="checkbox"], ' +
    '[role="radio"], [role="menuitem"], [role="combobox"], [role="option"], ' +
    '[contenteditable="true"]';
  const sensitiveRe = new RegExp(
    '(password|passwd|pwd|secret|token|passphrase|api[_-]?key|apikey|' +
    'one[-_]?time[-_]?code|otp|totp|cvv|cvc|cc-)',
    'i'
  );
  const out = [];
  let i = 0;
  for (const el of root.querySelectorAll(sels)) {{
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    i += 1;
    el.setAttribute('data-mcp-ref', String(i));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const nameAttr = el.getAttribute('name') || '';
    const idAttr = el.getAttribute('id') || '';
    const autoAttr = el.getAttribute('autocomplete') || '';
    const isProtected = (
      (tag === 'input' && type === 'password') ||
      sensitiveRe.test(nameAttr) ||
      sensitiveRe.test(idAttr) ||
      sensitiveRe.test(autoAttr)
    );
    let rawLabel = '';
    if (isProtected) {{
      rawLabel = '[PROTECTED FIELD]';
    }} else if (tag === 'select') {{
      const opts = Array.from(el.options || []);
      const selOpt = opts.find((o) => o.selected);
      const selText = selOpt ? (selOpt.text || selOpt.value || '').trim() : '';
      const preview = opts.slice(0, 5).map((o) => (o.text || o.value || '').trim()).filter(Boolean).join(' | ');
      rawLabel = (selText ? `selected=${{selText}} ` : '') + (preview ? `[options: ${{preview}}]` : '');
    }} else {{
      rawLabel = (el.innerText || el.value || el.placeholder ||
        el.getAttribute('aria-label') || el.title || '');
    }}
    const flags = [];
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') {{
      flags.push('[disabled]');
    }}
    if ((tag === 'input' && (type === 'checkbox' || type === 'radio')) || el.hasAttribute('aria-checked')) {{
      const isChecked = Boolean(el.checked) || el.getAttribute('aria-checked') === 'true';
      flags.push(isChecked ? '[checked]' : '[unchecked]');
    }}
    const label = rawLabel.trim().replace(/\\s+/g, ' ').slice(0, {limit});
    const flagStr = flags.length ? ' ' + flags.join(' ') : '';
    out.push(`[${{i}}] <${{tag}}${{type ? ' type=' + type : ''}}>${{flagStr}} ${{label}}`.trim());
  }}
  return {{ found: true, text: out.join('\\n') }};
}}
"""


DEFAULT_SNAPSHOT_JS = build_snapshot_js()
