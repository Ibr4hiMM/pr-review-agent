#!/bin/sh
# Assemble the public site (GitHub Pages) from site/ plus the dashboard's shared landing assets.
set -eu
cd "$(dirname "$0")/.."
out="${1:-_site}"
static=src/pr_review_agent/ui/static
rm -rf "$out"
mkdir -p "$out"
cp site/index.html site/site.js "$out/"
cp "$static/tokens.css" "$static/landing.css" "$static/landing.js" "$out/"
cp docs/dashboard.png "$out/"
touch "$out/.nojekyll"
echo "site built in $out"
