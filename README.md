# YuanLab

Personal toolkit site (Hugo), published to GitHub Pages at https://syuan33.github.io/YuanLab/.

- `content/toolkit/`: one page per tool (definition, deployment, usage). Start a new one with `hugo new toolkit/<name>.md`.
- `layouts/`, `static/css/`: the site's own minimal theme (no external theme).
- `serial-llm-gateway/`: OpenAI-compatible single-slot inference gateway ([toolkit page](https://syuan33.github.io/YuanLab/toolkit/serial-llm-gateway/)).
- `scripts/`: misc scripts.

Preview locally: `hugo server`, then open http://localhost:1313/YuanLab/.
Deploys run from `.github/workflows/hugo.yml` on every push to `master`.
