{ pkgs }: {
  # System Chromium for Playwright (JS rendering). The app finds it on PATH.
  deps = [ pkgs.chromium ];
}
