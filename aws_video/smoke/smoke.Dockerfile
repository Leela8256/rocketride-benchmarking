# Smoke client — RocketRide SDK only, pinned to the version the PDF
# benchmark verified against.
FROM --platform=linux/amd64 python:3.12-slim-bookworm
# rocketride drives the RR arm; requests drives the LG arm (lg_driver.py).
RUN pip install --no-cache-dir "rocketride==1.3.0" "requests>=2.31" "requests-toolbelt>=1.0"
WORKDIR /app
COPY smoke_video.py .
