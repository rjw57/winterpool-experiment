# Digital Winter Pool Prototype

## Concept

The bundled script will look in an incoming folder in Google drive for PDF
files, run them through the pool processing pipeline and write output to another
folder. An output index PDF and summary CSV are written to the processed file
folder.

## Preparing

Create a service account and download JSON-formatted credentials for it. Add the
email address associated with that service account as owner on the Tem drives
containing the incoming and output folders.

The JSON credentials should be present in a file called ``credentials.json``.

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
    -v $PWD/credentials.json:/credentials.json:ro \
    rjw57/winterpool-experiment --spec=/jobspec.yaml
```

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
