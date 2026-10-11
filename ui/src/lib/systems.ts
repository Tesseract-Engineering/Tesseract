// Source systems: display name, Simple Icons slug, brand color, monogram fallback.
// Simple Icons no longer ships Slack or the Microsoft 365 marks (the CDN 404s),
// so those render as brand-colored monograms directly instead of a failed request.

export interface SystemInfo {
  key: string;
  name: string;
  slug: string | null;
  color: string;
  mono: string;
  isCode?: boolean;
}

const SYS: Record<string, Omit<SystemInfo, "key">> = {
  jira: { name: "Jira", slug: "jira", color: "#0052CC", mono: "J" },
  slack: { name: "Slack", slug: null, color: "#4A154B", mono: "S" },
  github: { name: "GitHub", slug: "github", color: "#181717", mono: "G" },
  sharepoint: { name: "SharePoint", slug: null, color: "#0078D4", mono: "SP" },
  outlook: { name: "Outlook", slug: null, color: "#0078D4", mono: "O" },
  teams: { name: "Teams", slug: null, color: "#6264A7", mono: "T" },
  notion: { name: "Notion", slug: "notion", color: "#000000", mono: "N" },
  drive: { name: "Google Drive", slug: "googledrive", color: "#4285F4", mono: "GD" },
  zendesk: { name: "Zendesk", slug: "zendesk", color: "#03363D", mono: "Z" },
  docs: { name: "Docs", slug: "googledocs", color: "#4285F4", mono: "D" },
  code: { name: "Code", slug: null, color: "#37352F", mono: "<>", isCode: true },
  catalog: { name: "Service catalog", slug: "backstage", color: "#37352F", mono: "C" },
  org: { name: "Service catalog", slug: "backstage", color: "#37352F", mono: "C" },
  rma: { name: "RMA", slug: null, color: "#787774", mono: "R" },
  clio: { name: "Clio", slug: null, color: "#1C4DFF", mono: "Cl" },
};

export function systemOf(source: string | undefined): SystemInfo {
  const key = (source || "").toLowerCase();
  const known = SYS[key];
  if (known) return { key, ...known };
  const name = key ? key.charAt(0).toUpperCase() + key.slice(1) : "Source";
  return { key, name, slug: null, color: "#787774", mono: name.charAt(0) };
}

export function logoSrc(sys: SystemInfo, hex?: string): string | null {
  if (!sys.slug) return null;
  return `https://cdn.simpleicons.org/${sys.slug}/${(hex ?? sys.color).replace("#", "")}`;
}
