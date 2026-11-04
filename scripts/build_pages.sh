#!/bin/sh
# Copies the in-app guide into docs/ so GitHub Pages can serve it as a static site.
set -eu
cd "$(dirname "$0")/.."
sed \
  -e 's|<a href="./" data-nav="primary">Analyze</a>|<a href="https://github.com/globel420/DopplerTrack">GitHub</a>|' \
  -e 's|<a href="how-it-works" aria-current="page">How it works</a>|<a href="./" aria-current="page">How it works</a>|' \
  app/static/how-it-works.html > docs/index.html
cp app/static/styles.css docs/styles.css
cp app/static/how-it-works.js docs/how-it-works.js
touch docs/.nojekyll
