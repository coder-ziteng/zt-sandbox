# All-in-one sandbox (Bailian parity): code interpreter + browser in a single instance.
#   :49983  envd  (files + ConnectRPC commands)
#   :49999  jupyter (run_code)
#   :3000   browser (Chromium CDP / Playwright)
FROM sandbox/base:v1

ENV SBX_FEATURES=envd,jupyter,browser

EXPOSE 49983 49999 3000
