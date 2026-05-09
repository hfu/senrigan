set shell := ["bash", "-cu"]

build:
  npm run build

dev:
  npm run dev -- --host

preview:
  npm run preview -- --host
