# The pinger — reliable 10-minute renders

`render.yml` has a `schedule:` cron, but GitHub's scheduler is best-effort: runs
drift 5–20 minutes late under load and occasionally skip. The board wakes every
30 minutes (`REFRESH_SECONDS = 1800`) and only pays for a panel refresh when the
frame's SHA changed, so a *stale* frame just means the board burns a wake for
nothing. The pinger keeps the published frame fresh by triggering the render
workflow on a real 10-minute cadence.

It works by calling GitHub's **`workflow_dispatch`** API. That's the only moving
part: `pinger.sh` POSTs to the dispatch endpoint; a scheduler runs it every
10 minutes.

`render.yml` already has both `schedule:` and `workflow_dispatch:`, so nothing
in the repo changes. The schedule stays as a free backup — `concurrency:
cancel-in-progress` means a dispatch and a scheduled run can never overlap, and
public repos have unlimited Actions minutes, so the extra runs cost nothing.

---

## 1. Make a token (you must do this — it's a credential)

Create a **fine-grained personal access token** scoped to *only this repo*:

- GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**
- **Resource owner:** your account · **Repository access:** *Only select repositories* → `goes-paper`
- **Permissions → Repository permissions → Actions: Read and write**
  (that single permission is all the dispatch API needs)
- **Expiration:** set a calendar reminder to rotate it; GitHub will email before it lapses.

Copy the token (`github_pat_…`). It is a password — never paste it into the repo,
`pinger.sh`, a commit, or a chat. It only ever goes into the scheduler's secret
field below.

## 2. Pick where it runs

### Option A — cron-job.org (recommended: $0, no machine to keep on)

A free cloud cron that can POST with custom headers, so you don't even need
`pinger.sh` — you configure the HTTP call directly:

- **URL:** `https://api.github.com/repos/BraytonMiles/goes-paper/actions/workflows/render.yml/dispatches`
- **Method:** `POST`
- **Request body:** `{"ref":"master"}`
- **Headers:**
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer github_pat_…`   ← your token, stored in cron-job.org
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Schedule:** every 10 minutes
- **Expected response:** `204` (No Content) = success. cron-job.org can alert you
  if it ever stops returning 2xx.

### Option B — a machine that's always on (Raspberry Pi, NAS, home server)

Not your Mac unless it never sleeps — a laptop that sleeps is a bad pinger host.
On a box that's always up:

```sh
# put the token in the crontab's env, NOT in the script or the repo
crontab -e
```
```cron
*/10 * * * * GITHUB_TOKEN=github_pat_… /path/to/goes-paper/pinger.sh >> /tmp/goes-pinger.log 2>&1
```

Test it once by hand first:
```sh
GITHUB_TOKEN=github_pat_… ./pinger.sh
```
You should see `dispatched render.yml on master …`, and a new run under the repo's
**Actions** tab within a few seconds.

## 3. Confirm it's working

- Repo **Actions → render** should show runs firing ~every 10 min, labeled as
  `workflow_dispatch` (not just `schedule`).
- `https://braytonmiles.github.io/goes-paper/latest.sha` should change within
  ~10–15 min windows rather than drifting.

---

## Security notes

- The token grants **write to this repo's Actions** — enough to trigger workflows.
  Fine-grained + single-repo keeps the blast radius tiny, but still treat it like
  a password: rotate on the expiry reminder, and revoke immediately if the
  scheduler is ever compromised (GitHub → Settings → Developer settings → the
  token → Revoke).
- Nothing secret lives in this public repo. `pinger.sh` is safe to commit; the
  token is not, and never touches the repo.
