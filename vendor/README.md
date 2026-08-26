# vendor/

`ext-apps-app.js` is the bundled browser client from
[@modelcontextprotocol/ext-apps](https://github.com/modelcontextprotocol/ext-apps)
v1.7.5 (`dist/src/app-with-deps.js`, MIT licensed), vendored so the chart UI
resource can ship as self-contained HTML with no build step and no runtime
network dependency. Update by re-running:

    npm pack @modelcontextprotocol/ext-apps@<version> -O /tmp
    tar xzf /tmp/modelcontextprotocol-ext-apps-<version>.tgz -O package/dist/src/app-with-deps.js > vendor/ext-apps-app.js
