# Tesseract UI

The web app over the [Tesseract API](../docs/API.md). Built with Vite, React 18, TypeScript, Tailwind 4, shadcn/ui and Biome.

```bash
corepack pnpm install
VITE_API_URL=http://localhost:8091 corepack pnpm dev    # http://localhost:5173
corepack pnpm test && corepack pnpm lint && corepack pnpm build
```

The UI names no company and no people itself. It loads `GET /company` on start: the company name, the users who can sign in (each mapped to a viewer preset), and the starter questions. Signing in picks the viewer every answer is computed for.

| Path | What it does |
|---|---|
| `src/lib/api.ts` | the API client and response types |
| `src/lib/company.ts` | the company, sign-in users, and the remembered user |
| `src/lib/present.ts` | turns an `/ask` response into the view: answer lines with citations, the source rail, the timeline, people and actions |
| `src/components/Answer.tsx` | one answer: verdict, lines, design diagram, people and approvals |
| `src/components/ApprovalBlock.tsx` | proposed actions; approve runs propose → approve → execute against the API |
| `src/components/DesignDiagram.tsx` | the system design of existing work, for build requests |
| `src/components/rail/` | sources, restricted stubs, and each source's full text |
