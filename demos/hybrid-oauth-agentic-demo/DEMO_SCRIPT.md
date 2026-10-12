# Step-by-Step Live Demo Script: Multi-Agent Hybrid OAuth (2-Legged & 3-Legged) Handoff

**GCP Project:** `gemini-enterprise-demo-508317`  
**Internal Organization:** `brianreevz.altostrat.com`  
**Target User (AE):** `brian@brianreevz.altostrat.com`  
**Secondary User (RBAC Test):** `dave@brianreevz.altostrat.com`  
**Remote Vendor Platform (Mock Xvantage):** `bigredreeves.com`

---

## 0. Pre-Demo Environment Setup (2 Minutes)

### Step 0.1: Add Local `/etc/hosts` Domain Aliases
Map the vendor and internal organization domains to `127.0.0.1`:
```bash
sudo sh -c 'echo "127.0.0.1 bigredreeves.com brianreevz.altostrat.com" >> /etc/hosts'
```

### Step 0.2: Launch the Stack
Using Docker Compose:
```bash
cd /usr/local/google/home/brianreevz/Projects/cloud-networking-solutions/demos/hybrid-oauth-agentic-demo
docker compose up --build -d
```
*(Or locally via `uv run` — see `README.md` for local multi-process startup).*

### Step 0.3: Open Two Browser Windows Side-by-Side
1. **Left Window (External Vendor Portal):**  
   `http://bigredreeves.com:8004/demo-admin` *(or `http://127.0.0.1:8001/demo-admin`)*
2. **Right Window (Internal Gemini Enterprise Copilot):**  
   `http://brianreevz.altostrat.com:8004/copilot` *(or `http://127.0.0.1:8004/copilot`)*

---

## 1. Act I: Establish Baseline State & 3-Legged Identity Context

1. **Point to the Right Window (`brianreevz.altostrat.com/copilot`):**
   - Highlight the top-right green identity badge:
     `Authenticated as: brian@brianreevz.altostrat.com (3-Legged Token Active)`
   - Explain that Brian has logged in via **3-Legged OAuth (`authorization_code` flow)** with scopes `quotes:read quotes:write`.
   - In the left sidebar **"My Active Quotes (3-Legged RBAC Filtered)"**, point out **Quote `#1042`** for **Acme Corp**:
     - Line Item: `SKU-NET-MERAKI-MR36` (Qty: `20`)
     - Baseline Wholesale Cost: `$450.00`
     - Quoted Unit Price: `$540.00` (`16.67%` gross margin, `$10,800.00` total)
   - Note that **Quote `#9999`** (owned by `dave@brianreevz.altostrat.com` for Cyberdyne) is **completely invisible** to Brian's session due to strict server-side RBAC filtering.

2. **Point to the Left Window (`bigredreeves.com/demo-admin`):**
   - Show the **Mock Xvantage Vendor Distribution Portal**.
   - Highlight the two baseline inventory SKUs:
     - `SKU-NET-MERAKI-MR36` | Baseline Wholesale: `$450.00`
     - `SKU-SRV-DELL-R750`   | Baseline Wholesale: `$3,200.00`
   - Explain that our headless **2-Legged Sentinel Agent (`sentinel-agent`)** is continuously polling `/api/v1/promotions/active` every 5 seconds using a **machine-to-machine (`client_credentials`)** OAuth token (`client_id: xvantage-sentinel-app`, `scope: catalog.read promotions.read`).
   - Emphasize **Least Privilege**: This background agent has **zero write access** (`403 Forbidden`) to customer quotes—it can only detect promotions and notify the human account executive.

---

## 2. Act II: Trigger the Vendor Promotion & Observe the 2-Legged Sentinel Detection

1. **In the Left Window (`bigredreeves.com/demo-admin`):**
   - Click the red button next to `SKU-NET-MERAKI-MR36`:
     **[Trigger 20% Vendor Promo]**
   - Watch the Active Wholesale price drop immediately from `$450.00` to **`$360.00 (20% OFF)`** (`PROMO-MERAKI-20`).

2. **Watch the Automatic 2-Legged to 3-Legged Handoff (Within 5 Seconds):**
   - Within 5 seconds, the headless `sentinel-agent` polls `/api/v1/promotions/active`, discovers the 20% discount on `SKU-NET-MERAKI-MR36`, matches it against open **Quote `#1042`**, and dispatches both a Google Chat Webhook card and an In-App Notification stream event.
   - Look at the **Right Window (`brianreevz.altostrat.com/copilot`)**:
   - An amber **Notification Toast** slides into view at the top of Brian's Copilot workspace:
     > *"Promo detected on SKU-NET-MERAKI-MR36 (20% off). Your Quote #1042 for Acme Corp is eligible for repricing."*

---

## 3. Act III: 3-Legged Copilot Margin Analysis & Human-Confirmed Quote Mutation

1. **Click the Notification Toast in `brianreevz.altostrat.com/copilot`:**
   - Clicking the toast automatically populates the Gemini Copilot chat input with:
     > *"Show me the impact of the new vendor promotion on my Quote #1042 and reprice it to maintain an 18% margin."*
   - Click **Send** (or press `Enter`).

2. **Inspect the MCP Tool Execution & Before/After Margin Card:**
   - Gemini Copilot invokes two MCP tools on `mcp-server`:
     - `list_my_active_quotes(user_token=<3-Legged-Bearer>)`
     - `get_promo_details(promo_code='PROMO-MERAKI-20')`
   - Review the side-by-side comparison displayed by Gemini:
     - **Before Promo (Baseline):** Cost: `$450.00` | Quoted Price: `$540.00` | Margin: `16.67%` (`$10,800.00` total)
     - **After 20% Promo (18% Target Margin):** Cost: `$360.00` | New Quoted Price: **`$439.02`** | Margin: **`18.00%`** (`$8,780.40` total — saving Acme Corp **`$2,019.60`** while increasing margin!)

3. **Click `[Confirm & Update Quote #1042]`:**
   - Gemini invokes the third MCP tool using Brian's 3-legged user token:
     `reprice_quote(1042, "PROMO-MERAKI-20", 18.0, user_token)`
   - Look at the **My Active Quotes** and **Immutable Security & Quote Audit Trail** panels on the left:
     - Quote `#1042` is now updated to **`$439.02`** (`18.00%` margin).
     - The Audit Trail explicitly records:
       `[200_UPDATED] QUOTE_REPRICED | Quote #1042 | updated_by: brian@brianreevz.altostrat.com`

---

## 4. Act IV: Live RBAC & Automated Security Posture Verification

1. **Demonstrate Tenant Isolation Live in the UI:**
   - Click the button **[Test RBAC Isolation: Attempt to Mutate Dave's Quote #9999]** in the left sidebar.
   - Observe the `403 Forbidden` response in the chat window and the `[403_FORBIDDEN] PATCH_QUOTE_TENANT_VIOLATION` entry in the Security Audit Trail.

2. **Run the Automated Security Verification Script in the Terminal:**
   ```bash
   uv run python verify_security_posture.py
   ```
   Show the audience all three automated security assertions (`2-Legged Least Privilege`, `3-Legged Tenant Isolation`, and `Audit Trail User Attribution`) passing with `100%` compliance.
