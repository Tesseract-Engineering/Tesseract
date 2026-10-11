// Locate a claim's quote inside the full document text so it can be highlighted.

function clean(quote: string): string {
  return quote
    .replace(/^[…\s]+|[…\s]+$/g, "")
    .replace(/^\d+:\s/, "")
    .trim();
}

function find(text: string, needle: string): [number, number] | null {
  if (needle.length < 8) return null;
  let i = text.indexOf(needle);
  if (i < 0) i = text.toLowerCase().indexOf(needle.toLowerCase());
  return i < 0 ? null : [i, i + needle.length];
}

export function findQuote(text: string, quote: string): [number, number] | null {
  const q = clean(quote);
  if (!q || !text) return null;
  const direct = find(text, q);
  if (direct) return direct;
  // A quote cut mid-word on either side: drop the partial words.
  const trimmed = q.replace(/^\S*\s/, "").replace(/\s\S*$/, "");
  const inner = find(text, trimmed);
  if (inner) return inner;
  // Whitespace may differ (wrapped lines): match on a collapsed copy.
  const collapse = (s: string) => s.replace(/\s+/g, " ");
  const map: number[] = [];
  let flat = "";
  for (let k = 0; k < text.length; k++) {
    if (/\s/.test(text[k]) && /\s$/.test(flat)) continue;
    flat += /\s/.test(text[k]) ? " " : text[k];
    map.push(k);
  }
  const hit = find(flat, collapse(trimmed)) ?? find(flat, collapse(trimmed).slice(0, 60));
  if (!hit) return null;
  const endFlat = Math.min(flat.length - 1, hit[0] + collapse(trimmed).length - 1);
  return [map[hit[0]], map[endFlat] + 1];
}
