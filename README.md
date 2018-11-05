# Digital Winter Pool Prototype

## Concept

The bundled script will look in an incoming folder in Google drive for PDF
files, run them through the pool processing pipeline and write output to another
folder. An output index PDF and summary CSV are written to the processed file
folder.

## Preparing

Create/obtain a client id and secret an OAuth2 client as described in the
[Google quickstart
guide](https://developers.google.com/drive/api/v3/quickstart/python). Download
this to a file named ``client_secrets.json``.

Create a ``jobspec.yaml`` file in the current directory based on the [example
template](jobspec.example.yaml). The incoming and processed folder ids may be
cut-and-paste from URLs in the browser.

## Building the docker image

The script is best run from a Docker image since it requires a lot of additional
software installed:

```bash
$ docker build -t rjw57/winterpool-experiment .
```

## Running

Run the script via:

```bash
$ docker run --rm -it \
    -v $PWD/jobspec.yaml:/jobspec.yaml:ro \
    -v $PWD/client_secrets.json:/client_secrets.json:ro \
    -v pool-credentials-store:/store \
    -p 8080:8080 \
    rjw57/winterpool-experiment --auth-bind=0.0.0.0 --spec=/jobspec.yaml
```

This creates a persistent docker volume named "pool-credentials-store" for the
authorisation tokens and launches the tool. On first run the tool will ask you
to open a web-browser, log in and paste an authorisation token.

**AT THIS POINT THE TOOL ACTS AS YOUR USER.** The tool cannot do anything you do
not have permissions to do yourself, including uploading/accessing files in the
drive.

## Deploying

An [example stack](stack.example.yaml) for deployment to a Docker swarm is
included. This assumes that one is running the [traefik](https://traefik.io/)
traffic manager and that one can serve traffic for the host
``pool.swarm.usvc.gcloud.automation.uis.cam.ac.uk``.

A docker config must be present which holds the jobspec and a docker secret must
be present with the client secrets. Authorisation tokens are stored in a
persistent volume. See the stack for details.

When first deployed, the tool will be waiting for authorisation from Google.
This is a little complex as Google only allows localhost as a redirect URI.
Hence one needs to reverse proxy the authorisation server. This can be done via
[mitmproxy](https://mitmproxy.org/):

```bash
$ mitmproxy -p 8080 --mode reverse:https://pool.swarm.usvc.gcloud.automation.uis.cam.ac.uk/
```
