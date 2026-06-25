# Provider logos (optional, drop-in)

The sidebar "Providers" section shows a coloured monogram for each detected gateway
controller by default. To show a real logo instead, drop an SVG here named after the
provider key:

    cilium.svg   envoy.svg   envoyproxy.svg   nginx.svg   istio.svg
    traefik.svg  kong.svg     haproxy.svg      contour.svg

The key is derived from the GatewayClass `controllerName` (see `provider_of` in
`code/model.py`). If a file is missing, the UI silently falls back to the monogram —
nothing breaks. Use official SVGs and mind each project's trademark guidelines.
