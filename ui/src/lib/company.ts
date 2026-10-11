// The company this deployment serves, and who can sign in. Both come from the
// API (GET /company), which reads them from the company file, so the UI never
// names a company or a person itself.

import { api } from "./api";

export interface User {
  id: string;
  name: string;
  first: string;
  team: string;
  init: string;
  bg: string;
  viewer: string;
  handle: string;
}

export interface Company {
  name: string;
  users: User[];
  suggestions: string[];
}

const AVATAR_BG = ["#DBEDDB", "#E7F3F8", "#FDECC8", "#E8DEEE", "#F4EEEE", "#FBF3DB", "#FAEBDD", "#EEE0DA"];

function slug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ".")
    .replace(/^\.|\.$/g, "");
}

function toUser(u: { name: string; team: string; viewer: string }, i: number): User {
  const words = u.name.split(/\s+/).filter(Boolean);
  return {
    id: slug(u.name),
    name: u.name,
    first: words[0] ?? u.name,
    team: u.team,
    init: words
      .slice(0, 2)
      .map((w) => w[0]?.toUpperCase() ?? "")
      .join(""),
    bg: AVATAR_BG[i % AVATAR_BG.length],
    viewer: u.viewer,
    handle: slug(u.name),
  };
}

export async function loadCompany(): Promise<Company> {
  const c = await api.company();
  return { name: c.name, users: c.users.map(toUser), suggestions: c.suggestions };
}

const KEY = "tesseract.user";

export function loadUser(company: Company): User | null {
  try {
    const id = localStorage.getItem(KEY);
    return company.users.find((u) => u.id === id) ?? null;
  } catch {
    return null;
  }
}

export function saveUser(user: User | null): void {
  try {
    if (user) localStorage.setItem(KEY, user.id);
    else localStorage.removeItem(KEY);
  } catch {
    // storage blocked: the session still works, it just won't be remembered
  }
}
