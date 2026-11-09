
## Part 5. Product-family parity: make Gridlytics look and feel like Boxlytics

The owner's requirement: Gridlytics is a sibling "creator product" of Boxlytics. Same landing page structure and tone, same fonts, colour scheme, spacing and typography scale, same component styles on every page, and the same Supabase and company set-up, with the fixes already made in Solarlytics. Under the owner's rule the sign-in model does not change: preassigned accounts, email and password, no Google, no sign-up.

The facts below were read directly from the attached Boxlytics source (`boxlytics-main/src/index.css`, `tailwind.config.js`, `index.html`, `components/layout/*`, `components/ui/*`, `pages/LandingPage.tsx`, `pages/LoginPage.tsx`, `components/layout/FounderLayout.tsx`, `SiteFooter.tsx`) and from Gridlytics (`frontend/src/styles/index.css`, `components/layout/*`, `components/ui/*`, `app/router.tsx`, `features/auth/login-page.tsx`). The live parity reviewers' findings (Part 4, area "parity") add the page-by-page detail; this part is the change plan.

### 5.1 Where the two products differ today

| Aspect | Boxlytics (reference) | Gridlytics today | Change |
|---|---|---|---|
| Font | Poppins 300–900 (Google Fonts), JetBrains Mono for code; `* { font-family: "Poppins" }` | Inter via `--font-sans`, system mono | Poppins + JetBrains Mono |
| Base size | `html { font-size: 15px }`, 14px under 1440 px, 16px over 1600 px | browser default 16px | adopt the three-step scale |
| Colour tokens | shadcn hsl tokens: `--primary: 170 63% 33%` (≈ #1f8a78), dark `#145f53`, light `#2ba98f`; `--background` white; `--border: 214.3 31.8% 91.4%`; `--radius: 0.5rem`; success/warning/info/destructive | own tokens: brand `#0f766e`, bg `#f6f7f9`, line `#e2e6ec`, ok/warn/bad/info | map tokens to Boxlytics' values; keep the energy-source colours (plan Part 9.7 rule) |
| Surfaces | white page, "glass" cards: `rgba(255,255,255,.72)`, 1 px `rgba(255,255,255,.85)` border, `0 10px 30px rgba(15,23,42,.06)` shadow, blur 10–12 px; KPI tiles `rounded-2xl border-slate-200/70 bg-white/90 p-5 shadow-sm` | grey page `#f6f7f9`, flat white cards `rounded-lg border-line` with a 1 px shadow | white page, glass cards, rounded-2xl tiles |
| Sidebar | fixed left, 264 px (`w-64`), white glass, `h-20` logo header with the PNG logo (`h-9`), items `rounded-xl px-4 py-3 text-sm font-semibold`, active item filled `#145f53` white text with a 4 px right accent bar `#1f8a78`, footer "© 2026 Boxlytics / Powered by CCB-FEA" | sticky aside, light, items `rounded-md px-2 py-1.5 text-sm`, active `bg-brand-soft text-brand`, grouped Fleet / Site with a site switcher | restyle the items and header; keep the groups and the site switcher (they are Gridlytics' information architecture) |
| Header | `h-20`, page title (`font-display font-bold`) and subtitle from a per-route map, bell and user menu as `rounded-xl bg-gray-100 border-gray-200` buttons, user name in `#145f53` | `h-14`, no title, tier chip, role badge, bell, user menu | `h-20` with the title/subtitle map; keep tier chip and role badge inside it |
| Buttons | `rounded-lg`, variants primary / secondary / outline / ghost / danger, sizes sm / md / lg, focus ring | `rounded-md`, variants primary / secondary / ghost / danger, sizes sm / md | align radius, variants and sizes |
| Inputs | `.input-field`: `rounded-md border-gray-300 px-3 py-2 text-sm`, focus border `#145f53` + ring `#1f8a78/20`; prominent variant | `inputClass` in `field.tsx`: `h-9 rounded-md border-line` | align focus colours and padding |
| Scrollbar, motion | 6 px scrollbar in brand colours; `fade-in`, `float`; framer-motion fade-ins on the landing page | none | add the CSS; framer-motion only on the landing page |
| Landing page | `/` is a marketing page: sticky translucent header with logo, nav (Features, Research, Why Boxlytics, Benefits, Contact), Sign in and "Get started free"; hero with stat badges and a product video; sections Features (MODULE 01 / 02 cards), audiences, research, why, speed, how it works, benefits, CTA band (`gradient-hero`), SiteFooter | `/` is the app shell (redirects to `/fleet`); no marketing page | add `LandingPage` at `/` for signed-out visitors (signed-in people get a "Dashboard" button, as in Boxlytics) |
| Legal and support | `/privacy`, `/terms`, `/cookie-policy`, `/contact`, cookie consent banner, SiteFooter with "Boxlytics — by OnyxLeaf", privacy mailto, "An OnyxLeaf product · boxlytics.onyxleaf.co" | none | port the four pages, the consent banner and the footer with Gridlytics' details |
| Sign-in page | gradient page (`from-blue-100 to-blue-50`), card with logo | plain centred card (lifted from Solarlytics) | restyle the frame only; wording and model unchanged (see 5.8) |
| Founder console | `FounderLayout`: white `w-60` aside, ShieldCheck icon, "Founder" badge, nav | Solarlytics-lifted markup under `.fc` | restyle the frame to `FounderLayout`; keep the lifted logic |
| `index.html` | title "Boxlytics - Packaging Intelligence Platform", `icon.png` + apple-touch-icon, Google Fonts preconnect | title "Gridlytics", `favicon.svg` | "Gridlytics - Energy Dispatch Intelligence Platform", PNG icon, fonts |
| Auth and tenancy | Supabase auth, profiles, companies, memberships, RLS, invites, tokens, Google | local mode only (lifted from Solarlytics): `platform.db`, founder console creates accounts, email + password | keep local mode as the default; add Solarlytics' optional supabase mode (5.11), never Google or sign-up |

### 5.2 Design tokens (Tailwind 4, CSS-first)

Replace the page neutrals and brand in `frontend/src/styles/index.css` with Boxlytics' values and keep the energy-source colours untouched. Gridlytics' own token names stay (every component uses `bg-surface`, `text-ink`, `border-line`, `bg-brand`), so the change is one file:

```css
@import "tailwindcss";

@theme {
  --font-sans: "Poppins", system-ui, sans-serif;
  --font-display: "Poppins", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, monospace;

  /* Boxlytics' shadcn palette, written as hex so charts (lib/colors.ts) can read them */
  --color-bg: #ffffff;             /* --background 0 0% 100% */
  --color-surface: #ffffff;        /* --card */
  --color-surface-2: #f1f5f9;      /* --secondary / --muted 210 40% 96% */
  --color-line: #e2e8f0;           /* --border 214.3 31.8% 91.4% */
  --color-ink: #020817;            /* --foreground 222.2 84% 4.9% */
  --color-ink-2: #334155;
  --color-muted: #64748b;          /* --muted-foreground 215.4 16.3% 46.9% */
  --color-brand: #1f8a78;          /* --primary 170 63% 33% */
  --color-brand-dark: #145f53;
  --color-brand-light: #2ba98f;
  --color-brand-ink: #f8fafc;      /* --primary-foreground */
  --color-brand-soft: #e6f4f1;
  --color-ok: #16a34a;             /* --success 142 76% 36% */
  --color-ok-soft: #dcfce7;
  --color-warn: #f59e0b;           /* --warning 38 92% 50% */
  --color-warn-soft: #fef3c7;
  --color-bad: #ef4444;            /* --destructive 0 84.2% 60.2% */
  --color-bad-soft: #fee2e2;
  --color-info: #1a9bd7;           /* --info 200 80% 50% */
  --color-info-soft: #dbeafe;
  --radius-card: 1rem;             /* rounded-2xl tiles */
  --radius-control: 0.5rem;        /* --radius */

  /* unchanged: one colour per energy source everywhere (plan Part 9.7) */
  --color-pv: #f59e0b; --color-pv-soft: #fef3c7;
  --color-battery: #0d9488; --color-battery-soft: #ccfbf1;
  --color-grid: #2563eb; --color-grid-soft: #dbeafe;
  --color-genset: #ea580c; --color-genset-soft: #ffedd5;
  --color-load: #475569; --color-load-soft: #e2e8f0;
  --color-unserved: #c026d3; --color-unserved-soft: #fae8ff;
  --color-reserve: #dc2626;
  --color-demo: #7c3aed;
}

html { font-size: 15px; }
@media (max-width: 1440px) { html { font-size: 14px; } }
@media (min-width: 1600px) { html { font-size: 16px; } }

body { background: var(--color-bg); color: var(--color-ink); font-family: var(--font-sans); font-feature-settings: "rlig" 1, "calt" 1, "tnum" 1; }

/* Boxlytics' glass surface and gradients, verbatim */
.glass-surface { background: rgba(255,255,255,.72); border: 1px solid rgba(255,255,255,.85); box-shadow: 0 10px 30px rgba(15,23,42,.06); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
.gradient-primary { background: linear-gradient(135deg, #145f53 0%, #1f8a78 100%); }
.gradient-hero { background: linear-gradient(135deg, #145f53 0%, #1f8a78 50%, #2ba98f 100%); }
::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-track { background: #fff; } ::-webkit-scrollbar-thumb { background: #1f8a78; border-radius: 3px; } ::-webkit-scrollbar-thumb:hover { background: #145f53; }
@keyframes fade-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
.animate-fade-in { animation: fade-in .2s ease-out both; }
```

Two checks before merging: (1) the amber `--color-warn` and red `--color-bad` text on white must stay at 4.5:1 for small text, so keep `text-warn`/`text-bad` for icons and badges and use `#b45309`/`#b91c1c` (`--color-warn-text`, `--color-bad-text`) for words, the way Solarlytics keeps `--warn-text`; (2) `lib/colors.ts` reads the tokens by name for ECharts, so the names must not change.

**Fonts** in `frontend/index.html` (as Boxlytics):

```html
<link rel="icon" type="image/png" href="/icon.png" />
<link rel="apple-touch-icon" href="/icon.png" />
<title>Gridlytics - Energy Dispatch Intelligence Platform</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
```

Factories on slow links: `display=swap` keeps text visible; if the owner prefers no third-party request, self-host the two families under `public/fonts` with `@font-face` and the same names, which changes nothing else.

### 5.3 The application shell

**Sidebar** (`components/layout/sidebar.tsx`): keep the Fleet / Site groups and the site switcher; change the frame and items:

```tsx
// aside (app-shell.tsx): fixed, 264 px, white glass, shadow
<aside className="fixed inset-y-0 left-0 z-50 hidden w-64 flex-col border-r border-line bg-white/70 shadow-lg backdrop-blur-xl lg:flex">
// logo header, 80 px tall, as Boxlytics
<div className="flex h-20 items-center border-b border-line px-6"><NavLink to="/fleet"><img src="/gridlytics-logo.png" alt="Gridlytics" className="h-9 w-auto" /></NavLink></div>
// items
const linkClass = ({ isActive }) => cn(
  "group relative flex items-center gap-3 overflow-hidden rounded-xl px-4 py-3 text-sm font-semibold transition-all",
  isActive ? "bg-brand-dark text-white shadow-lg" : "text-ink-2 hover:bg-surface-2");
// active accent bar inside the NavLink
{isActive && <span aria-hidden className="absolute inset-y-0 right-0 w-1 rounded-l bg-brand" />}
// footer
<div className="border-t border-line bg-surface-2 p-4 text-xs text-muted"><p className="font-semibold text-brand-dark">© {year} Gridlytics</p><p>An OnyxLeaf product</p></div>
```

The main column becomes `lg:ml-64` with `<main className="min-h-[calc(100vh-5rem)] p-6">`, as `MainLayout.tsx` does.

**Header** (`components/layout/top-bar.tsx`): `h-20`, a title and subtitle on the left from a route map, the existing tier chip, role badge, bell and user menu on the right styled as `rounded-xl border border-line bg-surface-2 hover:bg-line`. The title map for Gridlytics (mirrors Boxlytics' `pageMeta`):

| Route | Title | Subtitle |
|---|---|---|
| /fleet | Fleet | Every site at a glance: mode, last data, today's plan |
| /sites/:id | Overview | Where the site stands right now |
| /sites/:id/plan | Plan | The next 48 hours, step by step, with the reasons |
| /sites/:id/control | Control | Hold, pause, resume, and what the inverter was told |
| /sites/:id/telemetry | Telemetry | What the meters and the inverter reported |
| /sites/:id/forecasts | Forecasts | PV, load and outage forecasts and how they scored |
| /sites/:id/outages | Outages | Load-shedding schedule, hazard and the reserve |
| /sites/:id/battery | Battery | State of charge, health, trust and wear |
| /sites/:id/genset | Genset | Fuel, run hours and the requests sent |
| /sites/:id/savings | Savings | The ledger by lever and the rebuilt bill |
| /sites/:id/pilot | Pilot | Switchback calendar, criteria and gates |
| /sites/:id/devices | Devices | Inverter brand, data source and capabilities |
| /sites/:id/settings | Site settings | Assets, grid connection, tariff, data source |
| /alerts | Alerts | What needs a person |
| /reports | Reports | Daily sheets and monthly statements |
| /tariffs | Tariffs | The library, versions and approvals |
| /settings | Company settings | Members, API keys, audit |
| /account | My account | Your details and password |

`PageHeader` then stops drawing its own `h1` on pages that have a header title (keep its `document.title` effect and its action slot), or the two are merged: `PageHeader` passes title and lead to the top bar through the UI store. The second is the smaller change: `useUi` already exists.

### 5.4 Components

- `ui/button.tsx`: `rounded-lg`; variants `primary` (`bg-brand hover:bg-brand-dark text-white`), `secondary`, `outline` (`border-line text-ink-2 hover:bg-surface-2`), `ghost`, `danger`; sizes `sm px-3 py-1.5 text-sm`, `md px-4 py-2 text-sm`, `lg px-6 py-3 text-base`; `focus:ring-2 focus:ring-offset-2`; a `loading` prop with the spinner. Keep `buttonClass()` for Radix `asChild` targets, as Boxlytics' `buttonVariants()`.
- `ui/card.tsx`: `rounded-2xl border border-line/70 bg-white/90 p-5 shadow-sm` plus `glass-surface` on the dashboard-type pages; header `border-b pb-4 mb-4`; title `text-base font-semibold`.
- `ui/kpi-tile.tsx`: label `text-xs font-semibold uppercase tracking-[0.14em] text-muted`; value `text-3xl font-display font-bold text-ink`; sub-line `text-xs text-muted`. These are the exact classes of Boxlytics' Dashboard tiles.
- `ui/field.tsx` (`inputClass`): `w-full rounded-md border border-line bg-white px-3 py-2 text-sm placeholder:text-muted focus:border-brand-dark focus:outline-none focus:ring-2 focus:ring-brand/20 disabled:bg-surface-2 disabled:opacity-60`.
- `ui/dialog.tsx`, dropdowns, popovers: `rounded-xl bg-white shadow-lg border border-line`, `animate-fade-in`.
- Tables: `thead` cells `text-xs font-semibold uppercase tracking-[0.14em] text-muted`, rows `border-b border-line`, hover `bg-surface-2`.
- Toasts (`sonner`): `richColors` off; a custom class set with the card radius and the brand colours.
- Empty states (`ui/empty-state.tsx`): icon in a `rounded-2xl bg-brand-soft p-3`, title `font-semibold`, hint `text-sm text-muted`, one primary action.
- Badges: pill `rounded-full px-3 py-1 text-xs font-bold uppercase tracking-widest`, the "section eyebrow" style Boxlytics uses (`text-green-700 bg-green-50 border-green-200`), mapped to brand.

Each change is a one-file edit with an existing vitest beside it (`field.test.tsx`, `app-shell.test.tsx`) to extend with a class assertion, and the 375 px screenshots of the e2e check to compare before and after.

### 5.5 The landing page at `/`

Boxlytics renders `LandingPage` at `/` and `*`; signed-in people see a "Dashboard" button in its header. Gridlytics should do the same, with `/fleet` as the destination. Route change in `app/router.tsx`:

```tsx
{ path: "/", element: <LandingPage />, ErrorBoundary: RouteErrorBoundary },
{ path: "/login", element: <LoginPage />, ErrorBoundary: RouteErrorBoundary },
{ path: "/privacy", ... }, { path: "/terms", ... }, { path: "/cookie-policy", ... }, { path: "/contact", ... },
{ path: "/admin/*", ...page(() => import("@/features/ops/founder-app")) },
{ element: <AppShell />, children: [ { path: "fleet", ... }, ... ] },   // the app, unchanged, under the shell
{ path: "*", element: <LandingPage /> },
```

`features/marketing/landing-page.tsx`, section for section as Boxlytics (same components `FadeIn`, `StatBadge`, `FeatureCard`, `YouTubeEmbed` facade, same spacing `py-24 px-4 sm:px-6 lg:px-8`, same heading scale `text-4xl lg:text-5xl font-bold`, same eyebrow pills), with Gridlytics' content:

| Section (Boxlytics) | Gridlytics content |
|---|---|
| Sticky header: logo, nav Features · Research · Why Boxlytics · Benefits · Contact; Sign in; "Get started free" | logo, nav Features · How it works · Why Gridlytics · Benefits · Contact; **Sign in**; **Request access** (`mailto:` or `/contact`). There is no "Get started free": accounts are preassigned. |
| Hero: headline, sub-line, two CTAs, three StatBadges (Predicted BCT 2,847 N · Allowable load · Max stack), product video | "Cheaper power, every day, without touching the factory." Sub-line: "Gridlytics plans your battery, solar and generator against the DISCO's time-of-use tariff and the load-shedding schedule, sends the plan to the inverter, and proves the saving on the rebuilt bill." StatBadges: **Peak window covered** 100 % of plan days · **Reserve before an outage** held in 48 h plans · **Bill rebuilt** to the rupee against the DISCO bill. Demo video facade (owner's upload). |
| Features: MODULE 01 — BOX STRENGTH PREDICTOR, MODULE 02 — OPTIMIZED BOX SELECTOR | MODULE 01 — TOU-AWARE DISPATCH PLAN (48 h plan, reasons per step, inverter slot tables per brand); MODULE 02 — OUTAGE-AWARE RESERVE AND GENSET (feeder schedule, hazard, reserve, genset at minimum load); MODULE 03 — SAVINGS LEDGER AND BILL TRUTH (ledger by lever, switchback, the rebuilt bill). |
| Audiences ("One platform. Diverse audience.") | Factory owners (what it saves); plant operators (hold, pause, resume, nothing silent); analysts and consultants (reports, bills, tariff versions). |
| Research ("Validated models. Accurate predictions.", the ASME paper) | "Measured, not modelled": the switchback method, the 1-minute bill rebuild against NEPRA/DISCO tariff versions (SRO references), how the ledger withholds a claim it cannot prove. |
| Why Boxlytics ("No tool combines all four.") | Tariff-aware, outage-aware, battery-life-aware, bill-verified: "No inverter app does all four." |
| Speed ("Production-grade FEA. Browser-native speed.") | "A new plan every 15 minutes": re-solve on tariff change, outage, forecast bust; time-limited plans say so. |
| How it works (3 steps) | 1 Connect: inverter brand, grid connection, tariff, data source (or a CSV). 2 Plan: the plan, its reasons, the slot table for your inverter; advisory first, closed loop when you sign off. 3 Prove: native-settings days, switchback, the rebuilt bill, the monthly statement. |
| Benefits (six cards) | Lower peak-window import · Reserve before load shedding · Less genset fuel · Longer battery life · A bill you can check · Nothing hidden from the operator. |
| CTA band (gradient-hero) | "Plan first. Pay less." + Sign in / Request access. |
| SiteFooter | "Gridlytics — by OnyxLeaf"; Product, Company, Legal columns; privacy mailto `privacy@gridlytics.onyxleaf.co`; "© {year} Gridlytics. All rights reserved." · "An OnyxLeaf product · gridlytics.onyxleaf.co". |

Wording rules from the plan still apply on the marketing page: no "guarantee", no "AI" claims, savings named as measured ranges only when the owner has measured them; until then the StatBadges state what the product does, not what it saved.

Dependencies: `framer-motion` and `@headlessui/react` (the header's user menu) are not in Gridlytics' `package.json`; `framer-motion` is needed for the fade-ins, the menu can use the existing Radix dropdown. Reuse of Boxlytics' `LandingPage.tsx`, `SiteFooter.tsx`, `CookieConsent.tsx` and the legal pages is reuse of the owner's own product; record each file and the Boxlytics commit in `THIRD_PARTY_NOTICES.md`, as was done for the Solarlytics sign-in.

### 5.6 Legal and support pages

Port `PrivacyPolicy.tsx`, `TermsOfService.tsx`, `CookiePolicy.tsx`, `ContactPage.tsx` and `CookieConsent.tsx` with the product name, contact addresses and the data list changed (telemetry, tariffs, bills, plans, control writes; retention; the DISCO bill images if uploaded; Pakistan data location note). The consent banner only matters once analytics or embedded video exist; the YouTube facade keeps the page cookie-free until play.

### 5.7 The sign-in page

Keep the lifted page's text and behaviour exactly: email and password, no sign-up link, no provider buttons, the 429 wait, the overlay re-sign-in. Change only the frame in `components/layout/logo.tsx` (`AuthPage`): the Boxlytics gradient page (`min-h-screen bg-gradient-to-br from-brand-soft to-white`), the logo image, and the card as `rounded-2xl glass-surface p-8`. The line "Forgotten your password? Ask your company's administrator for a new one." must become true: see the finding on the missing admin password-reset route (Part 4) and `members` in the founder console.

### 5.8 The founder console

Wrap `features/ops/founder-app.tsx` in a `FounderLayout` like Boxlytics': white `w-60` aside with the ShieldCheck icon, the product name, a "Founder" pill (`bg-brand/10 text-brand`), the nav (Overview, Companies, Members, Audit, Settings), the person's email and a sign-out at the foot; content on `bg-surface-2`. The `.fc` component styles in `index.css` already map the lifted markup to tokens, so the restyle is the frame plus the token change in 5.2.

### 5.9 Meta, icons and names

- `public/icon.png` (512 px) and `public/gridlytics-logo.png` (wordmark, as `boxlytics-logo-2.png`); keep `favicon.svg` as a fallback.
- `index.html`: title, description, Open Graph tags (`og:title`, `og:description`, `og:image` = `/hero-image.png`), `lang="en"`.
- `lib/wording.ts` `pageTitle()`: "Plan · Gridlytics" pattern unchanged.

### 5.10 Supabase and company set-up, under the owner's rule

What to keep: Gridlytics' local mode is the Solarlytics model and the right default. Accounts, companies and memberships live in `platform.db`; the founder console creates companies and people and hands out credentials; nobody signs up, invites or onboards themselves; sign-in is email and password.

What to add so the "Supabase settings" match Boxlytics with Solarlytics' fixes, without Google or sign-up:

1. **`GRIDLYTICS_AUTH_MODE=local|supabase`** (default `local`), the exact counterpart of `SOLARLYTICS_AUTH_MODE`; the live parity reviewer's finding says whether the Gridlytics backend already carries the supabase branch of the lifted `auth` module or only the local branch.
2. **`supabase/` folder** in `gridlytics-backend`, ported from `solarlytics-backend/supabase/` with names changed (`grid_000_preflight.sql`, `grid_001_accounts_and_companies.sql`, `grid_001_rollback.sql`, `README.md`): profiles, companies, company_memberships, row-level security, `get_my_context()`, `company_list_members()`. The migration is shipped for review and applied only by the owner by hand, as the Solarlytics README says; no script applies it.
3. **Project settings checklist** (from Solarlytics' README, section 3): Allow new users to sign up **off**; Email provider on with passwords; Google and every other provider **off**; anonymous sign-ins off; minimum password length 10; asymmetric JWT keys (JWKS) preferred, else `GRIDLYTICS_SUPABASE_JWT_SECRET`; access-token expiry 3600 s; Site URL the web app's address; redirect URLs only `/reset-password`; **no `/auth/callback`, no `/invite/**`**. The backend refuses tokens whose `amr` lacks `password` or whose provider is not `email`.
4. **Env**: `GRIDLYTICS_SUPABASE_URL`, `GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY` (server only, never in the web app), `VITE_GRIDLYTICS_AUTH_MODE` for the web app to pick the session client; `.env.example` in both repositories; secrets never in `data/` or git.
5. **Web app**: the GATES source rule "no Supabase in the web app" stands for local mode; in supabase mode the web app signs in through the backend's session route exactly as Solarlytics' web app does, so no Supabase client library enters the web repository. Confirm against `solarlytics-frontend` (`features/auth`) before writing.
6. **Founder console**: the only writer in both modes (service-role key on the server). Member creation, password set/reset by the admin, disable and delete, seats, company switch.
7. **Deliberately not ported from Boxlytics**: Google OAuth and `/auth/callback`, `/register`, `/invite/:token` and the invite tables, `/onboarding/new-company` and `/onboarding/join-existing`, `/pending-verification`, magic links, token-economy tables. Say so in `docs/DESIGN_LOG.md` (a new round) so nobody re-adds them.

### 5.11 Architecture and repository shape

The live architecture reviewer's table (Part 4, area "parity-architecture") lists the gaps. The headline items to mirror from Boxlytics' backend repository and Solarlytics: a `README.md` that opens with what the product is and how to run it; `RUN_LOCALLY.md`, `DEPLOY.md`, `SYNC_WORKFLOW.md`, `TROUBLESHOOTING.md`, `CHANGELOG.md`; `run_server.py` (or keep `scripts/start_local.py` and document it); `Dockerfile` and `docker-compose.yml` for the API with a mounted data volume and the scheduler; `.github/workflows` running both suites on pull requests to `v1-build` (Solarlytics has one to copy); `.env.example`; a backup note for `data/` (SQLite files; stop the scheduler or use the online backup API).

### 5.12 How to prove parity

- A vitest `styles/tokens.test.ts` that parses `index.css` and asserts the brand, border, radius and font tokens equal the Boxlytics values above.
- The e2e screenshot run (`npm run e2e`) before and after, compared side by side with Boxlytics' Dashboard, Predictor and landing page at 1440 and 375 px; the checklist: font renders as Poppins, base size 15/14/16, white page, glass cards, 80 px header with title and subtitle, 264 px sidebar with the filled active item and accent bar, rounded-lg buttons, the landing page sections in order, the footer line.
- Accessibility does not regress: the audit's a11y JSON (unnamed buttons, unlabeled inputs, landmarks) stays at zero on every page.
