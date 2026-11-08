# U1. Boxlytics web app audit: the conventions map for the OnyxLeaf EMS frontend

Research note for the OnyxLeaf EMS build plan. Written 6 November 2026 by a planning subagent (task U1-boxlytics-frontend). Self-contained: a reader who has not seen the brief can use it alone.

Purpose. OnyxLeaf is about to build a new SaaS product (a TOU-aware, outage-aware, genset-aware energy management and dispatch optimiser for Pakistani C&I sites) as two repositories: a Python FastAPI backend and a React + TypeScript + Vite frontend. The owner wants the new frontend to follow the conventions of the existing Boxlytics web app, but as clean-room code: patterns may be copied, code may not, except the sign-in code, which the owner has already permitted for reuse. This note audits the Boxlytics web app and says what to mirror, what to avoid, and what the EMS frontend skeleton should look like.

What was audited. `C:/Users/SaadTahir/Videos/boxlytics` (read only; nothing was edited, built or run). Facts about the repository:

- Git: branch `main`, 144 commits, HEAD `c429bef` "Merge pull request #16 from OmerJauhar/remove-landing-claims", last commit dated 2026-09-30 17:55 -0400 by Muhammad Omer Jauhar Khan. Remote is `github.com/OmerJauhar/boxlytics`, a shared team repo (MIGRATION_RECORD.md lines 243-244). Other remote branches: Dev, backup-release2-main, boxlytics_v0, feat/box-strength-predictor, feat/multi-tenant-admin, feat/multi-tenant-admin-v2, feat/ui-v1-parity.
- `package.json` name is `boxalytics` (sic), version 0.0.0, `"type": "module"`.
- 175 files under `src/`; 16 test files; `node_modules` is NOT installed in this checkout, so nothing here was executed and all version numbers come from `package.json` ranges and `package-lock.json` resolved versions.
- Node on this machine: v22.13.0, npm 10.9.2 (`node --version`, run outside the repo).
- Line numbers below refer to the files at HEAD `c429bef`.

Also read, for the EMS side only: `Perfect-EMS_TOU_Dispatch_Deep_Review.md` lines 164-220 (product scope, architecture, pilot plan, pricing) and `Product 1 - TOU-Aware Energy Management and Dispatch.md` lines 438-466 (7 components, two delivery modes) and 557-576 (work packages WP1-WP12). Those sections name the screens the EMS frontend must eventually have: fleet/portfolio dashboards, per-site plan view (96 x 15-minute setpoints over 48 h), savings reports (daily and monthly rupee and diesel, counterfactual vs executed), alerting, tariff calendar, site onboarding (the static data model), and an installer white-label dashboard. Nothing in those documents prescribes a UI library or layout; the frontend conventions are therefore taken from Boxlytics alone.

Cross-reference only (not audited in depth): `C:/Users/SaadTahir/Videos/solarlytics/frontend/package.json` shows the owner's other product on React 18.3.1, Vite 5.4, TypeScript 5.6, react-router-dom 7.18, jspdf 4.2, with no Tailwind, no React Query, no Zustand and no vitest (tests run with `node --test`). Solarlytics is a much thinner stack than Boxlytics; the owner's "Boxlytics style" brief makes the Boxlytics stack the reference, and this note treats it that way.

---

## 1. Stack table with versions

Resolved versions are from `package-lock.json` (`packages["node_modules/<pkg>"].version`); ranges are from `package.json`. "Files using it" is a count of `src/**/*.ts(x)` files that import the package (grep on 6 Nov 2026, excluding the dead `src/App-*.tsx` files).

| Layer | Package | package.json range | Lock-resolved | Files using it | Notes |
|---|---|---|---|---|---|
| Runtime | react, react-dom | ^19.1.1 | 19.1.1 | all | README.md line 49 still says "React 18"; CLAUDE.md is right (19). |
| Language | typescript | ~5.8.3 | 5.8.3 | - | strict, noUnusedLocals, noUnusedParameters, erasableSyntaxOnly, noFallthroughCasesInSwitch, noUncheckedSideEffectImports (tsconfig.app.json lines 22-27), verbatimModuleSyntax (line 16). The `paths` block is declared twice (lines 9-11 and 28-30); harmless, tidy it in the new repo. |
| Build | vite, @vitejs/plugin-react | ^7.1.6, ^5.0.2 | 7.1.6 | - | `vite.config.ts` is 11 lines: react plugin + `@` alias to `./src`. No code splitting (`lazy(` appears 0 times in src). |
| Styling | tailwindcss 3, postcss, autoprefixer, @tailwindcss/forms | ^3.4.17 | 3.4.17 | - | shadcn-style HSL tokens in `src/index.css` lines 5-49; brand hex `#145f53` / `#1f8a78` / `#13d86c` also hard-coded inline 124 times across tsx files. |
| Class merge | clsx, tailwind-merge | ^2.1.1, ^3.3.1 | - | 1 (cn.ts) | `src/utils/cn.ts` (6 lines). |
| Routing | react-router-dom | ^7.9.1 | 7.9.1 | - | BrowserRouter, flat `<Routes>` in `src/App.tsx`; no nested layouts, no data router. |
| Server state | @tanstack/react-query (+devtools) | ^5.89.0 | 5.89.0 | 26 | QueryClient in App.tsx lines 66-80 (5 min staleTime, no retry on 4xx). |
| Client state | zustand | ^5.0.8 | 5.0.8 | 3 stores | `auth.store.ts` (persist to localStorage), `founderAdmin.store.ts` (persist to sessionStorage), `app.store.ts` (not persisted). |
| HTTP | axios | ^1.12.2 | 1.12.2 | 1 (api.client.ts) | Only `analysis.service.ts` goes through axios; every other service calls Supabase directly. |
| Auth / DB | @supabase/supabase-js | ^2.87.1 | 2.87.1 | 6 | Email/password + Google OAuth; RPCs and tables called from the browser under RLS. |
| Forms | react-hook-form, @hookform/resolvers, zod | ^7.63.0, ^5.2.2, ^4.1.9 | 7.63.0, -, 4.1.9 | 10 / 7 | Used on auth, profile, onboarding and company forms only; the big calculator pages use plain `useState`. |
| Tables | @tanstack/react-table | ^8.21.3 | 8.21.3 | 1 | Only `components/box-selector/RecommendationsTable.tsx`; every admin/founder table is a hand-written `<table>`. |
| Charts | recharts | ^3.2.1 | 3.2.1 | 4 | Dashboard (LineChart), FounderOverviewPage (BarChart), BoxPredictor, BoxOptimizer. |
| 3D | three, @react-three/fiber, @react-three/drei | ^0.180.0, ^9.3.0, ^10.7.6 | - | 1 | `components/box-predictor/BoxPreview3D.tsx` (608 lines). Not relevant to EMS. |
| Headless UI | @headlessui/react | ^2.2.8 | 2.2.8 | 6 | Header menu, ui/Accordion, ui/Dialog, ui/Switch, ui/Tabs, LandingPage. |
| Radix | @radix-ui/react-{accordion, alert-dialog, checkbox, dialog, label, popover, radio-group, select, tooltip} | ^1.x / ^2.x | - | 4 | Only alert-dialog, dialog (as `radix-dialog.tsx`), popover, tooltip are imported. accordion, checkbox, label, radio-group, select are installed but unused (the same-named files in `components/ui` are hand-rolled). |
| Icons | lucide-react | ^0.544.0 | 0.544.0 | 70 | The only icon set. |
| Toasts | sonner | ^2.0.7 | 2.0.7 | 25 | `<Toaster />` mounted once in App.tsx line 355; `toast.success/error/info` everywhere. |
| Motion | framer-motion | ^12.23.16 | 12.23.16 | 4 | Landing, Contact, CookieConsent, SimulationHistory only. |
| PDF | jspdf | ^3.0.4 | 3.0.4 | 1 | `services/pdf-export.service.ts` (2,081 lines, four hand-drawn report generators, `new jsPDF` at lines 375, 1126, 1374, 1882). |
| PDF (unused) | html2canvas | ^1.4.1 | 1.4.1 | 0 | Declared, never imported. |
| Tests | vitest, jsdom, @testing-library/react, @testing-library/jest-dom | ^3.2.7, ^20.0.3, ^16.3.2, ^6.9.1 | 3.2.7 | 16 files | `vitest.config.ts`: jsdom, globals, setup file, `src/**/*.{test,spec}.{ts,tsx}`. jsdom 20 is old (jsdom 31 is current). |
| Lint | eslint 9 flat config, typescript-eslint, eslint-plugin-react-hooks, eslint-plugin-react-refresh, globals | ^9.35.0, ^8.43.0, ^5.2.0, ^0.4.20, ^16.4.0 | 9.36.0 | - | `eslint.config.js` (36 lines). No Prettier, no lint-staged, no husky, no .editorconfig, no .nvmrc. |
| Hosting | vercel.json | - | - | - | One SPA rewrite rule `/(.*) -> /index.html`. |
| Side scripts | Python / Node | - | - | - | `scripts/generate_materials.py`, `scripts/start_local.py` (starts backend on 8012 and web on 5190, per its docstring lines 10-14), `scripts/smoke.mjs` (playwright-core smoke over 5 routes, lines 23-29; playwright-core is not in package.json, so how it was run is UNVERIFIED). |

Scripts in `package.json` lines 6-15: `dev`, `build` (= `tsc -b && vite build`), `lint`, `preview`, `test` (= `vitest run`), `test:watch`, `generate:materials`, `generate:materials:check`. There is no `typecheck` script; SYNC_WORKFLOW.md lines 82-91 warn that bare `npx tsc --noEmit` compiles nothing because `tsconfig.json` is solution-style (`"files": []`), so the gate must be `npx tsc --noEmit -p tsconfig.app.json`.

Fonts: `index.html` lines 10-13 load Poppins and JetBrains Mono from Google Fonts; `tailwind.config.js` lines 9-13 map `sans`/`display` to Poppins and `mono` to JetBrains Mono; `src/index.css` line 56 forces `font-family: "Poppins"` on `*`. README.md line 53 still says Inter (stale).

---

## 2. Folder and route map

### 2.1 Folder layout (`src/`, 175 files)

```
src/
  main.tsx                 23 lines: createRoot + AppWithAuth wrapper that calls useAuthStore().initializeAuth() once
  App.tsx                  361 lines: QueryClient, BrowserRouter, every <Route>, CookieConsent, Toaster, ReactQueryDevtools
  App-debug.tsx, App-minimal.tsx, App-simple.tsx, App-simple-working.tsx, App-test.tsx   DEAD (see section 6)
  index.css                244 lines: Tailwind layers, HSL tokens, glass-surface rules, .input-field component class
  vite-env.d.ts
  assets/react.svg         unused scaffold leftover
  config/runtime.ts        API_BASE_URL, BACKEND_ENABLED, buildApiUrl(), getBackendDisabledMessage()
  components/
    ui/                    21 primitives: Button, Card, Input, Select, Checkbox, Switch, Slider, Dialog (headless), radix-dialog,
                           alert-dialog, tooltip, popover, label, radio-group, Accordion (headless), accordion-new (hand-rolled),
                           Tabs (headless, UNUSED), FieldHelp, RouteErrorBoundary
    layout/                MainLayout, Sidebar, Header, SiteFooter, FounderLayout, AdminLayout (UNUSED)
    auth/                  LoginForm, RegisterForm, RequireAuth, RequireActiveMembership, RequireCompanyAdmin, RequireFounder,
                           RequireSuperAdmin (UNUSED), OnboardingGate
    workflow/              WorkflowTabs (+ WorkflowNavigation), BatchSelector, AnalysisSavedDialog
    box-selector/, box-predictor/, optimizer/, cost/, engineering/, environment/, library/, materials/, shipping/, help/
                           product-module components (one folder per module)
    CookieConsent.tsx, TokenCostHint.tsx, AnalysisVisualizations.tsx, BoardCombinationSVG.tsx, ManufacturingCostInputs.tsx,
    PredictorManufacturingSection.tsx, PredictorShippingSection.tsx      loose module components at the folder root
  pages/                   route-level components (one file per route); subfolders admin/ (UNROUTED), company/, founder/, onboarding/
  services/                api.client, supabase.client, auth, membership, billing, founderAdmin, analysis, simulation, costHistory,
                           boardLibrary, materialCatalog, weather, pdf-export
  stores/                  auth.store, founderAdmin.store, app.store
  hooks/                   usePricing (the only custom hook file)
  types/                   index.ts (316 lines), analysis.types.ts, optimizer.types.ts
  utils/                   about 20 pure helpers (formatters, costCalculations, loadDisplacementCurve, ...) with co-located *.test.ts
  data/                    materials, costMaterials, generated/materials.generated.ts (written by scripts/generate_materials.py)
  test/setup.ts            jest-dom + window.matchMedia stub
```

Outside `src/`: `public/` (24 MB of landing images, logos, sample results), `docs/` (3 md), `migrations/` (12 SQL + SUPABASE_SETUP.sql), `supabase/` (edge functions `send-invite`, `founder-admin`, shared `cors.ts` and `founder-auth.ts`), `db/`, `boxlytics_models/` (Python model code shipped inside the frontend repo), `scripts/`, and 11 one-off patch scripts plus 4 scratch files at the root (section 6).

Import style is mixed: 173 `@/`-alias imports vs 302 relative imports (`../` 233, `./` 69); 22 files are tab-indented and 150 space-indented; 60 components use `React.FC`, the newer ones use plain function components. There is no formatter to settle this.

### 2.2 Route map (`src/App.tsx` lines 91-351)

| Path | Guard(s) | Layout | Page | App.tsx lines |
|---|---|---|---|---|
| `/` | none | none | LandingPage | 93 |
| `/login`, `/register` | none | none | LoginPage, RegisterPage | 94-95 |
| `/auth/callback` | none | none | AuthCallback (Google OAuth return) | 96 |
| `/r/:id` | none | none | ResultView (public shareable result) | 97 |
| `/privacy`, `/terms`, `/cookie-policy`, `/contact` | none | none | legal + contact pages | 98-101 |
| `/onboarding/intent`, `/onboarding/new-company`, `/onboarding/join-existing` | RequireAuth > OnboardingGate expect=["needs-onboarding"] | none | IntentPage, NewCompanyPage, JoinExistingPage | 104-133 |
| `/pending-verification` | RequireAuth > OnboardingGate expect=["pending-verification"] | none | PendingVerificationPage | 134-143 |
| `/pending-assignment` | RequireAuth > OnboardingGate expect=["pending-assignment","rejected"] | none | PendingAssignmentPage | 144-153 |
| `/invite/:token` | none (page handles both signed-in and signed-out) | none | InviteAcceptPage | 156 |
| `/admin`, `/admin/companies`, `/admin/users`, `/admin/pricing`, `/admin/config`, `/admin/settings`, `/admin/audit` | RequireFounder (renders FounderLoginPage inline when no founder session) | FounderLayout | Founder* pages | 160-229 |
| `/company/members`, `/company/settings` | RequireCompanyAdmin | MainLayout | CompanyMembersPage, CompanySettingsPage | 232-251 |
| `/dashboard`, `/box-selector`, `/box-predictor`, `/box-optimizer`, `/profile`, `/simulation-history`, `/history` | RequireActiveMembership | MainLayout | product pages | 254-349 |
| `/box-cost-calculator`, `/help` | RequireAuth only (no membership needed) | MainLayout | BoxCostCalculator, HelpPage | 298-329 |
| `*` | none | none | LandingPage (there is no 404 page) | 350 |

Three product routes are additionally wrapped in `<RouteErrorBoundary routeName="...">` (lines 279, 291, 303) and given a `key` so navigating between them remounts state.

Referenced but undefined routes: `/forgot-password` (LoginForm.tsx line 186 links to it; no route), `/reset-password` (auth.service.ts line 158 uses it as the Supabase redirect; no route). The Header's "Settings" menu item (Header.tsx lines 162-177) has no `onClick`.

### 2.3 The protected shell

- `components/layout/MainLayout.tsx` (34 lines): `<TooltipProvider>` > `div.latest-dashboard.min-h-screen.bg-white` > `<Sidebar />` + `div.lg:ml-64` > `<Header />` + `<main className="min-h-[calc(100vh-5rem)] p-6">{children}</main>`. The provider is mounted here, not in App.tsx, for the reason recorded in MIGRATION_RECORD.md section 2.6.
- `components/layout/Sidebar.tsx` (216 lines): fixed left, `w-64`, hidden off-canvas below `lg` with a click-away overlay; `navigationItems` array (lines 16-53) of `{name, href, icon, module}`; active state by `location.pathname === item.href`; a conditional "Manage Company" link when `useCanManageCompany()` is true (lines 156-165); footer with copyright. Sidebar open/closed state lives in `app.store.ts` (`sidebarCollapsed`, `toggleSidebar`).
- `components/layout/Header.tsx` (221 lines): 80 px glass header; page title and subtitle from a `pageMeta` map keyed by pathname (lines 15-52); mobile hamburger; a notifications bell with no behaviour (lines 104-106); Headless UI `Menu` user dropdown (Profile, Settings, Help, Sign out).
- `components/layout/FounderLayout.tsx` (89 lines): a second, simpler shell for `/admin/*`, `NavLink` with `end` flags and `isActive` class callback (lines 44-60), "Back to app" link, sign-out from the founder store.
- Tabs inside a page are NOT done with the `components/ui/Tabs.tsx` Headless UI wrapper (0 importers). They are done with `components/workflow/WorkflowTabs.tsx` plus page-local `activeTab` state and `hidden` panels; section 8 describes this in full.

### 2.4 Auth store, guards and API client

`src/stores/auth.store.ts` (319 lines):
- Zustand `create<AuthStore>()(persist(...))`, storage key `auth-storage`, `partialize` keeps `user`, `token`, `isAuthenticated`, `currentCompanyId` (lines 207-215).
- Actions: `login(user, token)` then `loadContext()`; `logout()` calls Supabase signOut then clears; `initializeAuth()` reads the Supabase session on boot; `loadContext()` calls the `get_my_context` RPC via `membershipService.getMyContext()` and picks a default company (lines 174-201).
- Selector hooks exported from the same file: `useIsSuperAdmin`, `useCurrentMembership`, `useMyRoleInCurrentCompany`, `useCanManageCompany` (lines 222-247).
- `useAuthRoutingState()` (lines 274-319) derives a discriminated union `loading | unauthenticated | needs-onboarding | pending-verification | pending-assignment | rejected | active | super-admin-only` from primitive slices with `useMemo`; the comment at lines 263-273 records why (returning a fresh object from a Zustand selector caused infinite re-renders), and lines 283-288 record a second lesson: only the first context load may report `loading`, otherwise a balance refresh after a paid run unmounted the page.
- Dev bypass (lines 81-139): when `import.meta.env.DEV && import.meta.env.VITE_DEV_AUTH_BYPASS === "true"`, a mock user `dev-local-user`, token `dev-local-token`, and a mock active membership in company `dev-local-company` are set; `VITE_DEV_AUTH_EMAIL` and `VITE_DEV_AUTH_ROLE` (default `member`; set `owner` to see company-admin screens) tune it. The branch is dropped from production bundles because `import.meta.env.DEV` is a compile-time constant; MIGRATION_RECORD.md section 2.7 says the built `dist/` was grepped and had zero occurrences. `billing.service.ts` lines 94-100 short-circuit `spend()` under the same gate so the mock company is never charged. These three variables are NOT in `.env.example`.

Guards (`src/components/auth/`): `RequireAuth` (28 lines; `isLoading` text, else `<Navigate to="/login" state={{from}} replace />`), `RequireActiveMembership` (50 lines; a `switch` over `useAuthRoutingState().kind` that sends each state to its screen), `RequireCompanyAdmin` (44 lines; owner/admin or super-admin, else `/dashboard`), `OnboardingGate` (54 lines; `expect: RoutingKind[]`, redirects to the screen matching the actual state), `RequireFounder` (42 lines; verifies the founder token on mount and renders `FounderLoginPage` inline instead of redirecting), `RequireSuperAdmin` (40 lines; unused, superseded by RequireFounder).

API client `src/services/api.client.ts` (51 lines):
- `axios.create({ baseURL: API_BASE_URL || undefined, headers: {"Content-Type": "application/json"} })` (lines 8-13).
- Request interceptor (lines 16-37): rejects with `getBackendDisabledMessage()` when `BACKEND_ENABLED` is false; adds `X-API-Key: <VITE_API_KEY>` when set (lines 23-25); adds `Authorization: Bearer <token>` from `useAuthStore.getState().token` (lines 28-31).
- Response interceptor (lines 40-49): on HTTP 401 and `BACKEND_ENABLED`, calls `useAuthStore.getState().logout()` and hard-navigates `window.location.href = '/login'`. No refresh-and-retry, no `from` preservation.
- `src/config/runtime.ts` (37 lines): `API_BASE_URL` = trimmed `VITE_API_BASE_URL` with trailing slashes removed; `BACKEND_ENABLED = backendFlag !== "false" && API_BASE_URL.length > 0`; `buildApiUrl(path)`; and an unconditional `console.log('[Runtime Config]', {...})` at lines 10-16 marked "remove after testing" that ships to production.
- `src/services/supabase.client.ts` (23 lines): `createClient(url || 'https://placeholder.supabase.co', key || 'placeholder-anon-key', { auth: { autoRefreshToken: true, persistSession: true, detectSessionInUrl: true } })`; exports `SUPABASE_ENABLED`.
- Token staleness (inferred from code, not reproduced at runtime): the store copies `session.access_token` once at login/boot; `authService.onAuthStateChange` (auth.service.ts line 216) and `refreshToken` (line 139) exist but are called from nowhere in `src/` (grep on 6 Nov 2026). Supabase refreshes its own session in the background, but the axios interceptor keeps sending the first token, so after the JWT lifetime (Supabase default 1 h; UNVERIFIED for this project) backend calls would 401 and force a logout. Boxlytics rarely hits this because only `analysis.service.ts` uses axios; an EMS frontend that talks to FastAPI for everything would hit it within the hour.

`VITE_API_KEY` (`.env.example` line 4: `replace-with-your-backend-api-key`) is a static shared secret compiled into the public bundle and sent as `X-API-Key` on every request. It cannot authenticate a user and anyone can read it from the built JS; treat it as a coarse "which frontend build is this" tag at most.

Env variables (`.env.example`, 14 lines): `VITE_API_BASE_URL` (example value `https://api.onyxleaf.co/api/v1`), `VITE_ENABLE_BACKEND`, `VITE_API_KEY`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_APP_NAME`, `VITE_APP_VERSION`, `NODE_ENV`. `VITE_APP_NAME`/`VITE_APP_VERSION` are read nowhere in `src/` (grep). CLAUDE.md line 48 says the API base URL "defaults to http://localhost:8000/api/v1"; the code has no such default (`runtime.ts` line 1 defaults to empty string, which disables the backend).

---

## 3. Page inventory (one line each; line counts from `wc -l`)

Public:
- `pages/LandingPage.tsx` (1,561): marketing page with hero, benefits, workflow steps, demo video, pricing copy; framer-motion; links to /login, /register, /contact.
- `pages/LoginPage.tsx` (24) and `pages/RegisterPage.tsx` (24): gradient backdrop + "Back to Home" link around `LoginForm` / `RegisterForm`.
- `pages/AuthCallback.tsx` (52): reads the Supabase session after Google OAuth, calls `login()`, toasts, navigates to /dashboard or /login.
- `pages/ResultView.tsx` (438): public read-only view of a saved simulation by id (the `/r/:id` share link; QR codes generated in SimulationHistory).
- `pages/PrivacyPolicy.tsx` (328), `pages/TermsOfService.tsx` (282), `pages/CookiePolicy.tsx` (344): static legal text with `mailto:` contacts (privacy@/legal@ boxlytics.onyxleaf.co) and `SiteFooter`.
- `pages/ContactPage.tsx` (493): contact form that builds a `mailto:hello@boxlytics.onyxleaf.co?subject=...` link (line 100); no backend submit.
- `components/CookieConsent.tsx` (316): banner + preferences (essential always on, analytics, marketing) persisted to `localStorage["bxl_cookie_consent"]` with a version string (lines 15-49); no analytics SDK is wired to the choice.

Onboarding (auth required, no membership yet):
- `pages/onboarding/IntentPage.tsx` (78): choose "create a company" or "join an existing one".
- `pages/onboarding/NewCompanyPage.tsx` (113): zod-validated company name, calls `create_company_signup` RPC, owner lands in pending-verification.
- `pages/onboarding/JoinExistingPage.tsx` (112): zod-validated company name, calls `request_join_existing` RPC, user lands in pending-assignment.
- `pages/onboarding/PendingVerificationPage.tsx` (66): "waiting for approval" with refresh (`loadContext`) and sign-out.
- `pages/onboarding/PendingAssignmentPage.tsx` (118): waiting/rejected screen with `restart_onboarding` RPC and support email `support@onyxleaf.co`.
- `pages/InviteAcceptPage.tsx` (306): reads the invite token, shows company/role, calls `accept_invite` RPC; sends signed-out users to `/login?next=/invite/<token>`.

Product (MainLayout):
- `pages/Dashboard.tsx` (542): personal run counts, 7-day trend, average runtime, weekly recharts LineChart, module launch tiles; data from `simulationService.getUserResults` (React Query key `["dashboard-records", userId]`).
- `pages/BoxPredictor.tsx` (4,561): the flagship calculator; lettered tabs A-D + Results, demo inputs, JSON import/export, PDF export, token spend, save-to-history dialog. The reference page for section 8.
- `pages/BoxOptimizer.tsx` (6,157): same workflow pattern with tabs product/board/materials/shipping/manufacturing/results and recharts outputs.
- `pages/BoxCostCalculator.tsx` (2,954): same workflow pattern with tabs box/board/costs/results; results saved to `localStorage["boxlytics_cost_history"]` via `costHistory.service.ts`.
- `pages/BoxAnalyzer.tsx` (1,876): legacy FEA page; NOT routed in App.tsx (dead, confirmed by MIGRATION_RECORD.md section 2.10).
- `pages/BoxSelectorPage.tsx` (341) and `pages/BoxSelector.tsx` (9, re-export shim): rule-based recommendations rendered by the only `@tanstack/react-table` table; the sidebar link is commented out (Sidebar.tsx lines 23-28) but the route is live.
- `pages/SimulationHistory.tsx` (1,342): personal and company result lists, batch renaming, QR code and public link per result, PDF re-export; served at both `/simulation-history` and `/history`.
- `pages/ProfilePage.tsx` (310): react-hook-form + zod edit of first/last/company name via `authService.updateProfile` (Supabase user metadata); shows Google avatar.
- `pages/HelpPage.tsx` (26): wraps `components/help/HelpPanel.tsx` (274 lines of FAQ and analysis-file format reference).

Company admin (MainLayout, owner/admin only):
- `pages/company/CompanyMembersPage.tsx` (726): members table, invite dialog, revoke invitation, change role, remove member, transfer ownership, seats used.
- `pages/company/CompanySettingsPage.tsx` (345): rename company (react-hook-form), token balance and ledger (`billingService.getBilling`), transfer ownership.

Founder console (FounderLayout, separate login):
- `pages/founder/FounderLoginPage.tsx` (180): two-step username/password then 6-digit TOTP; credentials held in component state only.
- `pages/founder/FounderOverviewPage.tsx` (178): four StatCards + recharts BarChart of simulations per day + by-type bars.
- `pages/founder/FounderCompaniesPage.tsx` (210): searchable table (status, members, seats, simulations, token balance) + grant-tokens modal.
- `pages/founder/FounderUsersPage.tsx` (231): searchable users table + "View as" side drawer (profile, membership, recent simulations) via the `impersonate` action.
- `pages/founder/FounderPricingPage.tsx` (160): per-action token costs, token price USD, enforcement toggle.
- `pages/founder/FounderConfigPage.tsx` (425): founder-editable material and lattice catalogue (upsert/delete rows, "push to backend").
- `pages/founder/FounderSettingsPage.tsx` (113): generic key/value platform settings (boolean toggle or text input per row).
- `pages/founder/FounderAuditPage.tsx` (77): founder action log (login, settings-set, impersonate).

Super-admin console (UNROUTED, superseded by the founder console; kept in the tree):
- `pages/admin/AdminOverviewPage.tsx` (170), `pages/admin/AdminCompaniesPage.tsx` (575), `pages/admin/AdminUsersPage.tsx` (1,089) with `components/layout/AdminLayout.tsx` and `components/auth/RequireSuperAdmin.tsx`. No file outside `pages/admin` imports them (grep on 6 Nov 2026). Their features are still reachable through `membership.service.ts` RPCs and are listed in section 4 because they are the only UI for approving companies and placing users.

---

## 4. Admin, founder and company feature list

Every feature below is one service method (file and line) wired to one UI action. The backend for all of them is Supabase: Postgres RPCs under RLS (`migrations/001`-`012`), two Edge Functions (`supabase/functions/send-invite`, `supabase/functions/founder-admin`), and two tables read directly (`companies`, `company_invites`, `profiles`, `simulation_results`).

### 4.1 Self-service onboarding and membership (`services/membership.service.ts`, 283 lines)
- `getMyContext()` line 20: RPC `get_my_context` -> `{profile, memberships[], pending_invites_for_email[]}` (types/index.ts lines 60-64). The single call the auth store depends on.
- `createCompanySignup(name)` line 28 (RPC `create_company_signup`), `requestJoinExisting(name)` line 36, `restartOnboarding()` line 43.
- Invites: `createInvite(companyId, email, role)` line 110 calls Edge Function `send-invite` (which wraps RPC `create_invite` and emails via Resend; returns `invite_link` and a `warning` if email failed), `acceptInvite(token)` line 147, `revokeInvite(id)` line 155, `listCompanyInvites(companyId)` line 273 (direct table read).
- Company admin: `companyListMembers` line 265, `changeMemberRole` line 162, `removeMember` line 173, `transferOwnership` line 180.
- Roles: `CompanyRole = owner | admin | member`; `MembershipStatus = pending | active | suspended`; `AssignmentStatus = none | pending_assignment | rejected` (types/index.ts lines 16-18). Seat limits are enforced server-side (`seat_limit` on company).

### 4.2 Platform super-admin operations (same service; UI only in the unrouted `pages/admin/*`)
- `approveCompany(id, seatLimit)` line 50, `adminRejectCompany` line 244, `setSeatLimit` line 58, `suspendCompany` line 86, `reactivateCompany` line 93.
- `placeUser(userId, companyId, role)` line 66, `rejectUser` line 79, `adminReopenUser` line 237, `adminSetSuperAdmin(userId, bool)` line 257.
- Read views: `adminListPendingUsers` line 225, `adminListCompanies` line 231, `adminListDirectory` line 251 (one row per user x active membership), `listPendingCompanies`/`listVerifiedCompanies`/`listPendingAssignmentUsers` lines 193-221 (direct table reads).
- UI actions in `pages/admin/AdminCompaniesPage.tsx`: Approve company (line 314), Reject company (377), Adjust seats (434), Suspend company (502), Reactivate company (551). In `pages/admin/AdminUsersPage.tsx`: Place user (897), Reject request (1012), Re-open request (1063), toggle super-admin.

### 4.3 Founder console (`services/founderAdmin.service.ts`, 261 lines; `stores/founderAdmin.store.ts`, 77 lines)
- Transport: plain `fetch` POST to `${VITE_SUPABASE_URL}/functions/v1/founder-admin` with `apikey` + `Authorization: Bearer <anon key>` (gateway) and `x-founder-session: <token>` (the real credential), body `{action, ...}` (lines 134-157). The function runs with `verify_jwt = false` (supabase/config.toml lines 11-16) and uses the service-role key inside, so it sees everything regardless of RLS (supabase/functions/founder-admin/SETUP.md lines 3-14).
- Auth: `loginCredentials(username, password)` line 161 -> `{requires2fa}`; `loginTwoFactor(username, password, code)` line 168 -> `{token}`; `verifySession(token)` line 176 -> `{authenticated, expiresAt}`. Secrets are env vars on the function: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` (sha256 hex), `ADMIN_TOTP_SECRET` (base32), `ADMIN_SESSION_SECRET` (SETUP.md lines 24-40). The store keeps only `{token, expiresAt}` in sessionStorage key `bxl_founder_admin` (store lines 71-75).
- Data: `stats` line 184 (companies/users/memberships/simulations totals, last-30, by type, daily series), `companies` line 187 (with member_count, simulation_count, token_balance), `users` line 190, `settingsGet`/`settingsSet` lines 193-198 (table `platform_settings`), `auditLog` line 199 (table `admin_audit_log`), `impersonate(userId)` line 202 (read-only "view as": profile + membership + simulations).
- Token economy: `grantTokens(companyId, amount, note)` line 207 (negative removes), `companyBilling(companyId)` line 213, `pricingSet(costs, tokenPriceUsd, enforcement)` line 216.
- Catalogue: `configGet` line 229, `configMaterialUpsert`/`Delete` lines 235-243, `configLatticeUpsert`/`Delete` lines 244-252, `configRefreshBackend` line 253 (asks the function to push the catalogue to the Python backend; returns `{pushed, status, reason}`).
- Founder nav (FounderLayout.tsx lines 17-25): Overview, Companies, Users, Tokens & pricing, Materials & lattice, Settings, Audit log.

### 4.4 Token billing as seen by the app (`services/billing.service.ts`, 135 lines; `hooks/usePricing.ts`; `components/TokenCostHint.tsx`)
- `getPricing()` line 68: RPC `get_token_pricing` with hard-coded fallback `{cost: 0.2, predictor: 1, optimizer_per_combination: 1, analyzer: 1}`, `token_price_usd: 0.1`, `enforcement: true` (lines 60-64).
- `spend(companyId, action, units, meta)` line 88: RPC `spend_tokens`; maps the `INSUFFICIENT_TOKENS` error text to a typed `InsufficientTokensError` (lines 107-115). Called by the three calculators before a run.
- `getBilling(companyId)` line 119: RPC `get_company_billing` -> balance + ledger.
- `quote(pricing, action, units)` line 129: pure helper for the cost hint shown next to each Run button.
- Pricing is cached app-wide by `usePricing()` (React Query key `["token-pricing"]`, 10 min staleTime).

### 4.5 Results persistence (`services/simulation.service.ts`, 252 lines)
- `saveResult(userId, companyId, record)` line 69: inserts into table `simulation_results` when `SUPABASE_ENABLED`, else (or on error) falls back to `localStorage["bxl_sim_results"]` capped at 300 records (lines 30-57). Records carry `type`, `title`, `parameters`, `results`, `batch_number`, `batch_note`, `execution_time`.
- `getUserResults(userId)` line 123 merges Supabase rows with local-only rows; `getCompanyResults(companyId)` line 108 uses RPC `company_list_results` (joins runner identity); `updateBatch`, `getPublicUrl`, `getQRCodeImageUrl`, `downloadQRCode` power SimulationHistory.

---

## 5. Patterns to mirror (each with the file that shows it)

P1. Guard-then-layout route composition. Each `<Route element>` is `<Guard><Layout><Page/></Layout></Guard>` (App.tsx lines 254-263). Keep the idea (guards are components, layouts are components, pages know nothing about either) but express it once with react-router nested routes and `<Outlet/>` instead of repeating the wrapper 20 times (section 7).

P2. Routing state as a discriminated union derived in one hook, consumed by thin guards. `useAuthRoutingState()` in auth.store.ts lines 253-319 and the `switch` in RequireActiveMembership.tsx lines 20-47. Every guard is 30-50 lines because the decision logic lives in one place. Also keep the two lessons in its comments (stable selector slices + `useMemo`; "only the first context load is loading").

P3. Bootstrapping the session once in `main.tsx` (lines 7-17) and persisting only the minimum (`partialize`, auth.store.ts lines 209-214).

P4. One axios instance with request/response interceptors (api.client.ts) and one `runtime.ts` that turns env strings into typed constants with a `buildApiUrl()` helper. Mirror the shape; fix the three defects noted in section 6 (stale token, hard redirect, console.log).

P5. Dev auth bypass double-gated on `import.meta.env.DEV` (auth.store.ts lines 93-139, billing.service.ts lines 98-100). This is exactly the right way to make the UI runnable with no identity provider and it is provably absent from production bundles. The EMS frontend should keep it, and additionally gate the FastAPI side with a matching dev-only bypass so the two agree.

P6. Services as plain objects of typed async functions, one per backend concern, that throw `Error(message)` and never touch React (membership.service.ts, billing.service.ts, founderAdmin.service.ts). Components call them only through React Query.

P7. React Query conventions: hierarchical keys `["founder", "companies"]`, `["company", companyId, "members"]`, `["admin", "pending-users"]`; `enabled: !!token`; `useMutation` with `onSuccess: () => { toast.success(...); qc.invalidateQueries({ queryKey: [...] }) }` and `onError: (e) => toast.error(...)` (FounderCompaniesPage.tsx lines 21-38, FounderSettingsPage.tsx lines 64-78). The QueryClient default of "no retry on 4xx" (App.tsx lines 70-77) is worth keeping, but match on `error.response?.status` rather than on the message text.

P8. `getErrorMessage(error, fallback)` in utils/errors.ts (24 lines): one place that knows an axios error carries `response.data.detail` (which is exactly FastAPI's error shape). Keep it.

P9. Typed selector hooks exported next to the store (`useCanManageCompany` etc., auth.store.ts lines 222-247) so components never re-derive role logic.

P10. Lettered workflow tabs with status icons, a locked Results tab, Previous/Next navigation, sticky strip, and scroll-to-top on change: `components/workflow/WorkflowTabs.tsx` (121 lines), `utils/scrollToTop.ts` (57 lines, see its header comment for the UX reasoning), and the `predictorTabs` array in BoxPredictor.tsx lines 2517-2565. Section 8 shows how to carry it over without the 4,000-line page.

P11. `RouteErrorBoundary` per product route (components/ui/RouteErrorBoundary.tsx, 52 lines; App.tsx lines 279-282): a class component with `routeName` that renders a red card instead of a white screen.

P12. `Button` with `variant`/`size`/`loading` and an exported `buttonVariants()` class helper for `asChild` targets (components/ui/Button.tsx lines 19-54); `cn()` from clsx + tailwind-merge (utils/cn.ts). The `Input` wrapper that renders label, error and help text in one component (components/ui/Input.tsx lines 28-69) and `FieldHelp` with a "Why this matters" `<details>` (components/ui/FieldHelp.tsx) are good shapes for a product full of engineering inputs (tariff categories, SOC limits, fuel curves).

P13. Design tokens as HSL CSS variables mapped into `tailwind.config.js` (`primary: "hsl(var(--primary))"` etc., lines 16-61; `src/index.css` lines 5-49) so the brand colour is changed in one line. Mirror the mechanism; do not mirror the 124 inline `#145f53` literals.

P14. Separate founder console with its own session store in sessionStorage, its own layout, and an inline login instead of a redirect (RequireFounder.tsx, founderAdmin.store.ts). For the EMS this maps onto "OnyxLeaf operations console" later; the pattern of a second small shell is right.

P15. Pure-function `utils/` with co-located `*.test.ts` and real numeric regression tests (e.g. `utils/loadDisplacementCurve.test.ts` pins the "4x quarter-box factor" bug named in BACKEND_WIRING_CONTRACT.md lines 62-67). The EMS equivalents are tariff maths, savings accounting and plan-slot conversions; the owner's Solarlytics rule "every money calculation gets a golden test" (solarlytics/CLAUDE.md line 16) should apply here too.

P16. Auth forms (LoginForm.tsx 295 lines, RegisterForm.tsx 357 lines, AuthCallback.tsx 52 lines, auth.service.ts 225 lines, supabase.client.ts 23 lines): react-hook-form + zod, `useMutation(authService.login)`, `?next=` deep-link handling with an open-redirect guard (LoginForm.tsx lines 54-61), Google OAuth with the `/auth/callback` route. The owner has permitted reuse of sign-in code, so these five files can be copied and then trimmed: remove the fake "Magic Link" mode (LoginForm.tsx lines 99-113 toasts "Dummy magic link sent ... We will wire backend flow next"), the Boxlytics logo and the `#13d86c` green, and add the missing `/forgot-password` and `/reset-password` routes that `auth.service.ts` lines 156-174 already support.

P17. The docs habit. `BACKEND_WIRING_CONTRACT.md` is a frontend-side statement of what fields the backend must return, with units, read off the real model, with a "still to do when the backend lands" list (lines 148-165). `SYNC_WORKFLOW.md` lists the exact gate commands and expected test counts (lines 79-94). The EMS repos should start with the same two documents: an API contract per screen and a gates file.

---

## 6. Things to avoid

A1. Dead entry points: `src/App-debug.tsx`, `App-minimal.tsx`, `App-simple.tsx`, `App-simple-working.tsx`, `App-test.tsx` (11-66 lines each; scaffold-era "is React rendering" screens; not imported anywhere).

A2. One-off patch scripts committed at the repo root: `deterministic_fix.cjs`, `fix_bracket.cjs`, `parse_and_rebuild.cjs`, `patch_file.js`, `patch_layout.cjs`, `patch_layout_precise.cjs`, `patch_throughput.js`, `patch_tools.js`, `rewrite_tree.cjs`, `split_file.cjs`, `update_layout.js` (13-117 lines each). Every one does `fs.readFileSync('src/pages/BoxPredictor.tsx' | 'src/pages/Dashboard.tsx')` and regex-rewrites JSX. Plus scratch files `extract_form.txt` (1,284 lines), `temp_contact.txt` (491), `temp_layout.txt` (1,328), `restore_dashboard.diff` (766). They exist because the pages were too big to edit by hand. The EMS repo should have a `.gitignore` entry and a rule: no scripts that rewrite source files.

A3. Monolithic pages: BoxOptimizer.tsx 6,157 lines, BoxPredictor.tsx 4,561, BoxCostCalculator.tsx 2,954, pdf-export.service.ts 2,081, BoxAnalyzer.tsx 1,876, LandingPage.tsx 1,561, SimulationHistory.tsx 1,342. Each tab's panel is inline JSX toggled with `className={activeTab === "board" ? "" : "hidden"}` (BoxPredictor.tsx lines 2662, 2907, 2942, 3332), so all panels mount at once and the page holds 30+ `useState` calls. Section 8 gives the alternative.

A4. Unused dependencies: `html2canvas` (0 imports), `@radix-ui/react-accordion`, `-checkbox`, `-label`, `-radio-group`, `-select` (0 imports; the hand-rolled files of the same names are used instead), `three`/`@react-three/*` (one 3D preview; irrelevant to EMS). Also duplicate primitives: `ui/Dialog.tsx` (Headless) and `ui/radix-dialog.tsx` (Radix), `ui/Accordion.tsx` and `ui/accordion-new.tsx`, `ui/Tabs.tsx` (unused) vs `workflow/WorkflowTabs.tsx`. MIGRATION_RECORD.md section 2.3 explains the `dialog.tsx` vs `Dialog.tsx` case-collision that Windows cannot hold but Linux can; the EMS repo should pick one casing rule (lowercase kebab files, PascalCase exports) and one primitives library.

A5. Unrouted but kept code: `pages/admin/*` (1,834 lines), `components/layout/AdminLayout.tsx`, `components/auth/RequireSuperAdmin.tsx`, `pages/BoxAnalyzer.tsx`, `pages/BoxSelector.tsx` shim, `src/assets/react.svg`.

A6. Production console noise: `src/config/runtime.ts` lines 10-16 logs every env value on boot; `App.tsx` line 83 logs the backend-disabled message; 7 `console.log` calls in `src/` overall.

A7. The 401 handler (api.client.ts lines 43-46) logs out and hard-reloads to `/login`, dropping the `from` location, and there is no token refresh path (section 2.4). For the EMS, the interceptor must read the live Supabase session (`supabase.auth.getSession()` or an `onAuthStateChange` subscription that updates the store) and retry once after a refresh before logging out.

A8. `VITE_API_KEY` as a pseudo-credential (section 2.4). Do not carry it over as an auth mechanism; the FastAPI backend must verify the Supabase JWT (or its own session) per request.

A9. Business logic in the browser against Supabase directly. Boxlytics calls 25+ RPCs and tables from the client (membership, billing, simulation_results, platform settings), so authorization lives in Postgres policies and SQL functions across 12 migration files, and the founder console is a 2FA edge function. That was workable for a calculator app; an EMS whose core is a Python optimiser, forecasters and 1-minute telemetry should have ONE backend (FastAPI) own all reads and writes, with the browser holding only a Supabase JWT. The pattern to keep is the service-layer shape, not the Supabase-from-the-browser wiring.

A10. Header/Sidebar page titles keyed by pathname string (Header.tsx lines 15-52) and `currentModule` duplicated in `app.store.ts` (lines 6-12, 35-37) when the URL already says which module is active. Put the title in the route definition (`handle: { title }`) instead.

A11. Hand-written `<table>` markup repeated in six admin/founder pages while `@tanstack/react-table` is installed and used once. Pick one: for the EMS (fleet lists, event logs, plan slots, ledger) use react-table everywhere via one `DataTable` component.

A12. Inline brand hex colours (124 occurrences of `#145f53`, `#1f8a78`, `#13d86c` in tsx) and `style={{...}}` glass effects duplicated in Sidebar.tsx lines 93-98 and Header.tsx lines 67-72, next to a tokenised Tailwind config that already defines `primary`. Also `src/index.css` lines 111-122 restyles any element matching `.rounded-xl.border.border-gray-200.bg-white` into a glass card globally, which makes every card's look depend on an exact class combination; and `Card.tsx` applies a `card` class (line 11) that no stylesheet defines (grep of index.css finds no `.card {` rule).

A13. No CI. `.github/` holds only `copilot-instructions.md` (a scaffold checklist). Gates exist only as prose in SYNC_WORKFLOW.md lines 79-94. No Prettier, no lint-staged, no .nvmrc, no `typecheck` script, no coverage, no e2e in CI (the `scripts/smoke.mjs` playwright script needs a global Chrome path and is not in package.json).

A14. Test coverage is pure-utils only: 16 files, 225 `it()` blocks by grep (SYNC_WORKFLOW.md line 93 says 229; the difference is one-line and nested cases the grep misses). One component test (`components/environment/__tests__/EnvironmentalExposureFields.test.tsx`), zero page, route, guard, store or service tests, zero network mocking. The auth-routing union (P2) is exactly the kind of logic that should have had a table test.

A15. Documentation drift: README.md says React 18, React Router v6, Inter font, MIT licence (no LICENSE file exists), lists demo credentials (lines 229-236), and describes `components/dashboard`, `box-optimizer`, `simulations`, `styles` folders that do not exist. CLAUDE.md says the API base URL defaults to localhost:8000 (it does not). Keep CLAUDE.md short and generated from the real tree; let the gates file be the source of truth.

A16. The founder console secrets model (sha256 password hash + TOTP + HMAC session in function env vars, SETUP.md lines 24-40) is bespoke crypto in an edge function. For the EMS, make the operations console a role on the normal identity (Supabase user with `is_platform_admin`, or FastAPI role) and keep 2FA as a Supabase MFA setting, not custom code.

A17. Mixed conventions with no enforcement: tabs vs spaces (22 vs 150 files), `@/` vs relative imports (173 vs 302), `React.FC` vs function components (60 vs rest), single vs double quotes. Add Prettier + an import-order rule on day one.

A18. `public/` carries 24 MB of PNG/JPEG marketing images (six `benefit-*.png` at 1.1-1.4 MB each) in git; the EMS repo should keep marketing assets out of the app bundle repo or at least compressed (WebP/AVIF) and under 200 KB each.

---

## 7. Recommended skeleton for the EMS frontend repository

Working name used below: `ems-web` (repository), app name to be decided by the owner. Everything here is clean-room; only the five sign-in files from P16 may be copied.

### 7.1 Base dependencies with versions

Versions were read from the npm registry on 6 November 2026 (`registry.npmjs.org/<pkg>/latest` and the `time` field for major releases). Two columns: the registry's latest, and the pin this note recommends, with the reason.

| Package | Registry latest (6 Nov 2026) | Recommended pin | Why |
|---|---|---|---|
| react, react-dom | 19.3.1 (19.3.0 published 2026-09-09) | ^19.3.1 | Same major as Boxlytics; 19.3 has been out two months. |
| typescript | 7.0.2 (published 2026-07-08; dist-tag `latest`) | ~5.9.3 (published 2025-09-30), revisit at month 2 | TS 7 is the native-compiler line; typescript-eslint 8.72 and Vite's plugin support for it are UNVERIFIED here. 5.9.3 is the last 5.x and matches Boxlytics' strict flags without surprises. Last 6.x on the registry is 6.0.3. |
| vite | 8.3.6 (8.0.0 published 2026-03-12; engines node ^20.19 or >=22.12) | ^8.3.6 | Eight months old, three minors in; node 22.13 on this machine satisfies it. Fall back to ^7.3.0 (2025-12-15) if any plugin lags. |
| @vitejs/plugin-react | 6.2.1 (6.0.0 published 2026-03-12) | ^6.2.1 | Pairs with Vite 8. |
| tailwindcss, @tailwindcss/vite | 4.3.4 (4.0.0 published 2025-01-21) | ^4.3.4 via the Vite plugin | Tailwind 4 is almost two years old; CSS-first `@theme` tokens replace `tailwind.config.js` and keep the HSL-token idea from P13. Boxlytics is on 3.4.17; do not copy its config file. |
| react-router | 8.5.1 (8.0.0 published 2026-06-17) | ^7.18.5 (`react-router-dom` 7.18.5, published 2026-10-27, is now a thin re-export of `react-router` 7.18.5) | v7 is what Boxlytics uses and what the owner knows; v8's changes are UNVERIFIED here. Import from `react-router` (not `react-router-dom`) so a v8 move is a version bump. |
| @tanstack/react-query | 5.106.1 | ^5.106.1 | Same major as Boxlytics. |
| zustand | 5.0.16 | ^5.0.16 | Same major. |
| @tanstack/react-table | 9.3.1 (9.0.0 published 2026-08-04) | ^8.21.3, or ^9.3.1 if the DataTable is written fresh against v9 docs | Boxlytics uses 8.21.3; v9 is three months old. Decide once when writing `DataTable`. |
| recharts | 3.10.1 | ^3.10.1 | Same major. |
| zod | 4.6.7 | ^4.6.7 | Same major. |
| react-hook-form, @hookform/resolvers | 7.91.0, 7.0.3 (7.0.0 published 2026-10-31) | ^7.91.0, ^7.0.3 | resolvers 7 is six days old; if it breaks, ^5.2.2 (Boxlytics) still works with zod 4. UNVERIFIED. |
| axios | 1.21.0 | ^1.21.0 | Same major. Alternative: a 40-line `fetch` wrapper; axios is kept only for interceptor familiarity. |
| @supabase/supabase-js | 2.119.0 | ^2.119.0 | Auth only (no RPC/table calls from the browser; see A9). |
| lucide-react | 1.69.0 (1.0.0 published 2026-03-23) | ^1.69.0 | Boxlytics is on 0.544; 1.x renamed some icons, so copy nothing by name. |
| sonner | 2.0.8 | ^2.0.8 | Same. |
| clsx, tailwind-merge | 2.1.1, 3.7.0 | ^2.1.1, ^3.7.0 | Same. |
| @radix-ui/react-tabs, -dialog, -tooltip, -popover, -dropdown-menu, -select | 1.2.1, 1.3.1, 1.4.1, ... | latest 1.x/2.x | One primitives family (Radix) instead of Headless UI + Radix + hand-rolled; shadcn-style wrappers written fresh. |
| date-fns | 4.4.0 | ^4.4.0 | Needed for 15-minute slot and PKT time handling; use `date-fns-tz` or `Intl` for Asia/Karachi. |
| jspdf | 4.2.1 (4.0.0 published 2026-01-03) | defer | Savings reports should be rendered by the backend (WeasyPrint/ReportLab) from the same numbers the API serves, so the PDF and the screen cannot disagree. Add jspdf only if a client-side export is demanded. |
| Dev: vitest, @vitest/coverage-v8 | 5.1.0 (5.0.0 published 2026-09-03; engines node ^22.12 or ^24 or >=26; peer vite ^6.4, ^7, ^8) | ^5.1.0 | Works with Vite 8 and node 22.13. |
| Dev: jsdom | 31.0.0 | ^31.0.0 | Boxlytics pins 20.x. |
| Dev: @testing-library/react, jest-dom, user-event | 16.3.3, 7.0.1 | ^16.3.3, ^7.0.1, latest | Component tests for guards, tabs, forms. |
| Dev: msw | 3.0.3 (3.0.0 published 2026-09-28) | ^2.x unless 3 is verified | Network mocking for service and page tests; v3 is five weeks old. UNVERIFIED. |
| Dev: @playwright/test | 1.65.0 | ^1.65.0 | Replaces `scripts/smoke.mjs`; run in CI against `vite preview`. |
| Dev: eslint, @eslint/js, typescript-eslint, eslint-plugin-react-hooks, eslint-plugin-react-refresh, globals | 10.14.0, 10.0.1, 8.72.1, 7.1.1, 0.5.7, 17.14.0 | eslint ^10.14.0 if typescript-eslint 8.72 declares support (UNVERIFIED), else ^9.39 | Flat config as in Boxlytics' `eslint.config.js`, plus `eslint-plugin-import-x` for import order. |
| Dev: prettier | 3.9.10 | ^3.9.10 | Boxlytics has none; add `prettier-plugin-tailwindcss`. |
| Dev: @types/react, @types/react-dom, @types/node | 19.3.0, -, 26.6.6 | ^19.3.0, ^19.3.0, ^22 (match node 22 LTS) | - |

Node: pin `engines.node` to `>=22.12 <23` and add `.nvmrc` = `22`. (Vite 8 and Vitest 5 both require 22.12+; the machine has 22.13.0.)

Do NOT add: framer-motion (only the landing page used it), three/@react-three, html2canvas, @headlessui/react, @tailwindcss/forms (Tailwind 4 has `@plugin`), jspdf (see above).

### 7.2 Folders

```
ems-web/
  .github/workflows/ci.yml          typecheck, lint, format check, unit tests with coverage, build, playwright smoke
  .nvmrc  .editorconfig  .prettierrc  eslint.config.js  vite.config.ts  vitest.config.ts  playwright.config.ts
  tsconfig.json (solution) + tsconfig.app.json + tsconfig.node.json      copy the STRICT flags from Boxlytics tsconfig.app.json lines 22-27
  .env.example                      every VITE_* the code reads, including the dev bypass trio
  index.html  vercel.json
  docs/
    API_CONTRACT.md                 per screen: endpoint, request, response fields with units (the BACKEND_WIRING_CONTRACT.md habit)
    GATES.md                        exact commands and expected counts (the SYNC_WORKFLOW.md habit)
    CONVENTIONS.md                  this section, kept current
  public/                           favicon, logo; no marketing images
  src/
    main.tsx                        createRoot, providers, RouterProvider
    app/
      router.tsx                    createBrowserRouter: public layout, auth layout, app shell (Outlet), ops shell (Outlet); handle.title per route
      providers.tsx                 QueryClientProvider, TooltipProvider, Toaster, devtools in DEV only
      query-client.ts               defaults (staleTime, retry on status not message)
      env.ts                        zod-parsed import.meta.env -> typed `env` object (the runtime.ts idea, validated, no console.log)
    features/                       one folder per product area; each owns its pages, components, api, hooks, schemas, tests
      auth/                         login, register, callback, forgot/reset password, guards, auth.store, session hook  (P16 reuse lives here)
      onboarding/                   organisation create/join, pending screens (if the owner keeps the Boxlytics onboarding flow)
      sites/                        site list, site onboarding wizard (static data model: PV kWp, battery, genset curve, DISCO, feeder), site settings
      dashboard/                    fleet/portfolio overview: savings to date, sites by status, alerts
      plan/                         per-site 48-h plan view: 96 x 15-min setpoints, SOC trajectory, grid/genset bars, re-solve history
      telemetry/                    1-minute series charts, outage timeline, data-quality flags
      tariffs/                      tariff calendar per DISCO/category, TOU windows, ICP, diesel price; SRO change alerts
      savings/                      daily/monthly rupee and diesel reports, counterfactual vs executed, switchback A/B, bill rebuild
      alerts/                       alert list and rules
      org/                          members, roles, invites, billing (the company/* pages)
      ops/                          OnyxLeaf operations console (the founder console): fleet stats, orgs, users, settings, audit  (later)
    components/
      ui/                           button, input, select, checkbox, switch, dialog, alert-dialog, tooltip, popover, tabs, card, badge, skeleton,
                                    data-table (react-table), empty-state, field-help, page-header, stat-card, error-boundary
      layout/                       app-shell (sidebar + header + Outlet), ops-shell, public-shell, sidebar, header, site-switcher
      workflow/                     WorkflowTabs, WorkflowNavigation, useLetteredTabs (section 8)
      charts/                       recharts wrappers with the theme applied once: TimeSeriesChart, SlotBarChart, SocLineChart, SavingsBarChart
    lib/
      api/client.ts                 axios instance + interceptors (live token, refresh-once-then-logout, X-Request-Id)
      api/endpoints.ts              typed functions per endpoint, generated or hand-written against docs/API_CONTRACT.md
      errors.ts                     getErrorMessage (FastAPI `detail` aware)
      format.ts                     PKR, kWh, kW, litres, percent, 15-min slot labels, Asia/Karachi dates
      time.ts                       slot index <-> timestamp, 48-h horizon helpers
      cn.ts
    stores/                         ui.store (sidebar), auth.store (feature-owned, re-exported), selected-site.store
    types/                          api.ts (DTOs mirrored from the backend's Pydantic models), domain.ts
    test/                           setup.ts, msw handlers, render-with-providers helper, fixtures (a sample 96-slot plan, a tariff calendar)
    styles/                         index.css with @theme tokens (Tailwind 4), no global class hijacks
  tests/e2e/                        playwright: login (dev bypass), site list, plan page tabs, savings report
```

Rules worth writing into `docs/CONVENTIONS.md`:
- Files kebab-case, components PascalCase exports, one component per file, no `React.FC`, no default exports except route lazies.
- Imports: `@/` alias only, no `../`; order enforced by eslint.
- A page component is under 300 lines; anything larger becomes feature components.
- Every `fetch` goes through `lib/api`; no Supabase data calls from the browser (auth only).
- Every money/energy formatter and every slot-time conversion has a golden test.
- Route-level `lazy()` for every feature folder (Boxlytics has none; its single bundle includes three.js).
- No scripts that rewrite source files; no `App-*.tsx`; no committed scratch files.

### 7.3 Scripts (`package.json`)

```
"dev": "vite",
"build": "npm run typecheck && vite build",
"preview": "vite preview --port 4173 --strictPort",
"typecheck": "tsc --noEmit -p tsconfig.app.json",
"lint": "eslint . --max-warnings 0",
"format": "prettier --write .",
"format:check": "prettier --check .",
"test": "vitest run --coverage",
"test:watch": "vitest",
"e2e": "playwright test",
"gates": "npm run typecheck && npm run lint && npm run format:check && npm run test && npm run build"
```

### 7.4 Test set-up

- `vitest.config.ts`: `environment: "jsdom"`, `globals: true`, `setupFiles: ["./src/test/setup.ts"]`, `include: ["src/**/*.{test,spec}.{ts,tsx}"]`, coverage thresholds (start at lines 60 percent, raise), `css: false`.
- `src/test/setup.ts`: jest-dom matchers, `window.matchMedia` and `ResizeObserver` stubs (recharts needs ResizeObserver under jsdom), msw `server.listen()` / `resetHandlers()` / `close()`.
- `src/test/render.tsx`: `renderWithProviders(ui, { route, authState })` that mounts QueryClient (retry off), MemoryRouter and a seeded auth store.
- Required tests from day one: (a) table test of the routing-state union (every `kind` x every guard); (b) api client interceptor: adds live token, refreshes once on 401 then logs out; (c) `useLetteredTabs` (section 8): completion, locking, next/previous skipping disabled tabs, scroll-to-top not called on refused change; (d) golden tests for PKR/kWh formatters and slot-time conversion across the PKT offset; (e) one msw-backed page test per feature (site list renders rows from a fixture); (f) playwright: dev-bypass login reaches the dashboard, plan page tabs A-D navigate.

### 7.5 CI (`.github/workflows/ci.yml`)

One workflow on push and pull_request: `actions/setup-node` with `node-version-file: .nvmrc` and npm cache; `npm ci`; `npm run typecheck`; `npm run lint`; `npm run format:check`; `npm run test` (upload coverage artifact); `npm run build`; `npx playwright install --with-deps chromium` and `npm run e2e` against `vite preview` with `VITE_DEV_AUTH_BYPASS=true` and `VITE_API_BASE_URL` pointed at an msw-free mock (or at the backend repo's test server once it exists). Vercel preview deploys per PR remain as in Boxlytics (`vercel.json` SPA rewrite), with the same env variables set in the Vercel project; `VITE_DEV_AUTH_BYPASS` must never be set there (the DEV gate makes it inert anyway).

### 7.6 Env handling (`src/app/env.ts`)

Replace `runtime.ts` with a zod schema parsed once at module load: `VITE_API_BASE_URL` (url, required unless `VITE_ENABLE_BACKEND=false`), `VITE_ENABLE_BACKEND` (default true), `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_DEV_AUTH_BYPASS`, `VITE_DEV_AUTH_EMAIL`, `VITE_DEV_AUTH_ROLE`, `VITE_DEFAULT_TIMEZONE` (default `Asia/Karachi`). Parse failure throws a readable error at boot in DEV and renders a "misconfigured build" page in PROD; nothing is logged to the console. Keep `.env.example` complete (Boxlytics' is missing the bypass trio).

---

## 8. How a page with lettered tabs (A, B, C, ...) should be structured

### 8.1 What Boxlytics does today (the reference, with its costs)

- Tab strip: `components/workflow/WorkflowTabs.tsx` lines 35-75. Props `tabs: WorkflowTab[]`, `activeTab: string`, `onTabChange(id)`, `ariaLabel`. Each tab is `{id, label, status?: "complete"|"incomplete"|"error", disabled?}`; a status icon (check / alert / circle) precedes the label; the strip is `sticky top-0 z-20` with a blurred backdrop; disabled tabs are dimmed and ignore clicks; `role="tablist"`, `role="tab"`, `aria-selected`, `aria-controls="<id>-panel"` are set (but no element with that id exists, so the ARIA link is broken).
- Previous/Next: `WorkflowNavigation` lines 84-120 finds the nearest enabled tab in each direction and labels Next as `Next: <label>`.
- Page-side state in `pages/BoxPredictor.tsx`: `const [activeTab, setActiveTab] = useState<"box"|"board"|"shipping"|"manufacturing"|"results">("box")` (lines 361-363); a `predictorTabs` array built on every render with lettered labels `"A — Box Configuration"`, `"B — Board Configuration"`, `"C — Shipping & Environmental Conditions"`, `"D — Manufacturing & Advanced Settings"`, `"Results"` whose `status` is computed inline from form state and whose Results tab is `disabled: !analysisResults` (lines 2517-2565); `handleWorkflowTabChange` refuses Results until a run exists (toast "Run the prediction to unlock Results."), toggles `showResults`, sets the tab and calls `scrollToTop()` (lines 2567-2580); the strip is rendered with `activeTab={showResults && analysisResults ? "results" : activeTab}` (line 2616); panels are inline JSX inside one big `<Accordion>` each toggled by `className={activeTab === "board" ? "" : "hidden"}` (lines 2662, 2907, 2942, 3332); the run/progress block is shown only on the last lettered tab (lines 3382-3386).
- Costs: all panels mount at once (every input, chart and 3D canvas is live even when hidden); the page owns 30+ pieces of state; the tab id union, the labels, the completion rules and the lock rule are spread over 1,000 lines; nothing is in the URL, so refresh or a shared link always lands on tab A; nothing is unit-tested.

### 8.2 The structure for the EMS skeleton

1. Declare the tabs once, next to the page, as data:

```ts
// src/features/sites/onboarding/tabs.ts
export const SITE_ONBOARDING_TABS = [
  { id: "site",     letter: "A", label: "Site & connection" },
  { id: "assets",   letter: "B", label: "PV, battery & genset" },
  { id: "tariff",   letter: "C", label: "Tariff & billing regime" },
  { id: "controls", letter: "D", label: "Control mode & limits" },
  { id: "review",   label: "Review & activate", locked: true },
] as const;
export type SiteOnboardingTabId = (typeof SITE_ONBOARDING_TABS)[number]["id"];
```

The label shown is `${letter} — ${label}` when a letter exists; the type of tab ids is derived from the array, so a typo is a compile error (Boxlytics repeats the union by hand).

2. One hook owns the mechanics (`src/components/workflow/useLetteredTabs.ts`), tested in isolation:

```ts
useLetteredTabs({
  tabs,                       // the array above
  status: (id) => "complete" | "incomplete" | "error",   // derived from the form, supplied by the page
  isUnlocked: (id) => boolean, // e.g. review unlocked only when every lettered tab is complete
  syncToUrl: true,            // ?tab=assets, so refresh and deep links work
}) => { activeTab, select(id), next(), previous(), tabsWithStatus, canSelect(id) }
```

`select(id)` refuses a locked or disabled tab (returns false, the page decides whether to toast), otherwise updates the URL search param and calls `scrollToTop()`; `next()`/`previous()` skip disabled tabs exactly as `WorkflowNavigation` does today. URL sync uses react-router's `useSearchParams`, so the browser back button walks the tabs.

3. One panel component per tab, each in its own file, mounted only when active:

```tsx
// src/features/sites/onboarding/SiteOnboardingPage.tsx  (target: under 150 lines)
const panels: Record<SiteOnboardingTabId, React.ComponentType<PanelProps>> = {
  site: SitePanel, assets: AssetsPanel, tariff: TariffPanel, controls: ControlsPanel, review: ReviewPanel,
};
...
<WorkflowTabs tabs={tabsWithStatus} activeTab={activeTab} onTabChange={select} ariaLabel="Site onboarding" />
<section id={`${activeTab}-panel`} role="tabpanel" aria-labelledby={`${activeTab}-tab`}>
  <ActivePanel form={form} />
</section>
<WorkflowNavigation tabs={tabsWithStatus} activeTab={activeTab} onPrevious={previous} onNext={next} />
```

Only the active panel mounts; the `id`/`aria-labelledby` pair makes the ARIA relationship real (Boxlytics sets `aria-controls` to an id that does not exist).

4. Form state lives in one `react-hook-form` instance created in the page with a zod schema per tab (`z.object({ site: siteSchema, assets: assetsSchema, ... })`), passed down to panels via `FormProvider`. A tab's `status` is `form.formState.errors[tabId] ? "error" : touchedAndValid ? "complete" : "incomplete"`, computed by one `useTabStatuses(form, tabs)` helper rather than ad hoc boolean chains per tab (BoxPredictor.tsx lines 2521-2548). Unmounted panels keep their values because the form instance, not the panel, owns them (`shouldUnregister: false`, which is the default).

5. Draft persistence: autosave the form to the backend as a draft (PATCH `/sites/{id}/draft`) debounced, or at minimum to `sessionStorage` keyed by site id, so navigating away does not lose tab B. Boxlytics keeps everything in component state and loses it on refresh.

6. The last tab (Review) renders a read-only summary from the same form values, the Run/Activate button and the mutation progress; this is where Boxlytics puts the progress bar (lines 3382-3400). A result or plan view is a separate route (`/sites/:id/plan`), not a fifth tab, so "Results" never needs locking logic; the review tab is simply disabled until every lettered tab is complete.

7. Where lettered tabs are the right pattern in the EMS: site onboarding (A site, B assets, C tariff, D control mode), tariff editor (A category and season windows, B ICP and fixed charges, C net-billing settlement, D diesel), pilot/baseline set-up (A instrumented baseline window, B inverter mode capture, C switchback calendar). Where they are the wrong pattern: dashboards, the plan view and the savings report, which are not sequential and should use ordinary Radix `Tabs` or sub-routes (`/sites/:id/plan`, `/sites/:id/telemetry`, `/sites/:id/savings`) with the sidebar or a secondary nav.

8. Tests for the pattern (vitest + testing-library): the hook (lock, skip-disabled, URL sync, scroll-to-top only on an accepted change), the status helper (errors -> "error", untouched -> "incomplete"), and one page test that fills tab A, clicks Next, and asserts tab B mounted and tab A unmounted.

---

## 9. What the four named docs decide (summary, with lines)

- `BACKEND_WIRING_CONTRACT.md` (166 lines): the frontend's statement of the ML backend contract. 19 input fields with units (lines 16-26), 4 outputs each with 5th/95th bands (lines 32-45), "all forces are full-box newtons, do not rescale" with the regression test that pins it (lines 52-73), the `summary` field -> UI field mapping table with fallback aliases (lines 76-98), a one-call curve builder API (lines 101-120), what Python code ships in which repo (lines 123-146), and a to-do list for when the backend lands (lines 148-165). Decision to carry over: a contract doc per screen that names fields, units and fallbacks, written from the real backend, with a "still to do" list.
- `AUTHENTICATION_IMPLEMENTATION.md` (274 lines): an implementation summary of the Supabase auth work: email/password + Google OAuth, session persistence in localStorage, `/auth/callback`, profile in user metadata, no extra tables initially (architecture decisions lines 217-226), env vars (lines 160-166), a testing checklist (lines 237-249) and troubleshooting. Decisions: Supabase over custom auth; profile in metadata; toasts for every auth outcome. The EMS should keep Supabase Auth and Google OAuth and drop metadata-as-profile in favour of a backend `users` table.
- `SYNC_WORKFLOW.md` (100 lines): how UI changes travel between `boxlytics-ui-v1` (rooted at `src/latest/`) and `boxlytics` (rooted at `src/`) with an alias rewrite (lines 19-31); eleven files that deliberately differ (lines 33-50); trained weights never cross (lines 52-64); robocopy commands (lines 68-77); the exact gates and expected counts (lines 79-94). Decisions: a written gates list; the `tsc -p tsconfig.app.json` trap. The EMS should have one repo per app and no mirror step, but keep the gates file.
- `MIGRATION_RECORD.md` (255 lines): what actually happened when ui-v1 was merged into main. Lessons the EMS inherits: strict TypeScript is kept and loose code is fixed (section 2.2); never hold two files differing only in case (`dialog.tsx` vs `Dialog.tsx`, section 2.3); a root-level provider lost in a partial migration is invisible to typecheck (section 2.6); the dev auth bypass's design and its proof of absence from `dist/` (section 2.7); compare assets by content not mtime (section 2.9); unrouted pages exist (section 2.10); deliver via branch + PR, never push to main (section 4).

Related docs not in the brief but useful: `RELEASE_2_PORT_REPORT.md` section 5.4 (the `npx tsc --noEmit` silent no-op), `supabase/functions/founder-admin/SETUP.md` (founder console secrets), `supabase/README.md` (invite emails via Resend), `BACKEND_INTEGRATION.md` (the older job-submit-and-poll contract: POST `/api/v1/simulate/box`, GET `/api/v1/jobs/{id}` every 2 s, lines 22-33 and 70-78; `analysis.service.ts` line 311 now polls 300 x 2 s = 10 min).

---

## 10. Open questions for the owner

1. Multi-tenant model for the EMS: keep the Boxlytics company/owner/admin/member + invite + seat-limit + platform-approval flow (one more week of work and six more screens), or start with one organisation per customer created by OnyxLeaf and add self-service later? The pilot plan (3 sites, installer channel) suggests the latter for v1.
2. Identity: Supabase Auth again (JWT verified by FastAPI), or FastAPI-native sessions? This note assumes Supabase Auth for sign-in only.
3. Which five sign-in files may be copied verbatim: this note assumes `LoginForm.tsx`, `RegisterForm.tsx`, `AuthCallback.tsx`, `auth.service.ts`, `supabase.client.ts`. Confirm whether `auth.store.ts` (with the dev bypass) and the guards count as "sign-in code" or must be rewritten; the note treats them as patterns to rewrite.
4. Tailwind 4 (CSS-first) vs staying on Tailwind 3 to match Boxlytics' config file: the recommendation is 4; the owner may prefer sameness.
5. Who renders PDFs: backend (recommended) or browser (jspdf, as in Boxlytics)?
6. Whether the operations console (founder console equivalent) is in scope for v1 at all; the brief says admin dashboards come later.

---

## 11. Verification status

Verified by reading the files named above on 6 Nov 2026: every path, line number, count and version in sections 1-6, 8.1 and 9. Verified by live npm registry fetch on 6 Nov 2026: the versions and publish dates in section 7.1. Not verified (marked UNVERIFIED above): compatibility of typescript-eslint 8.72 with TypeScript 7.0 and ESLint 10; @hookform/resolvers 7 and msw 3 stability; how `scripts/smoke.mjs` was run; the Supabase JWT lifetime for this project; whether the stale-token 401 actually occurs at runtime (inferred from code only). Nothing in the Boxlytics repository was executed, built, installed, edited or committed.
