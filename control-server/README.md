# TMM Wallpapers upload configuration server

This service keeps per-phone upload policies and exposes them to the Android app.

```sh
python3 control_server.py
```

Put this service behind HTTPS at the same host configured by
`UploadConfig.controlUrl`. The app opens the configuration panel from the
hidden Files action after ten taps.

The panel is served at `/v1/device/panel` and is authenticated with the
device ID and token issued by `/v1/device/register`. All phones default to
backup disabled.
