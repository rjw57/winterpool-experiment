FROM python:3.7-alpine
WORKDIR /usr/src/app

ADD requirements.txt /usr/src/app
RUN apk --no-cache update \
    && apk --no-cache add swig texlive gcc g++ musl-dev libxml2-dev \
        libxslt-dev libstdc++ libjpeg-turbo-dev pulseaudio-dev poppler-utils \
        tesseract-ocr libressl-dev \
    && pip install -r requirements.txt \
    && apk del gcc g++ musl-dev libxml2-dev libxslt-dev libstdc++ \
        libjpeg-turbo-dev pulseaudio-dev libressl-dev

ADD tool.py /usr/src/app
ADD templates/ /usr/src/app/templates
VOLUME /usr/src/app/store

# For local OAuth2 auth webserver
EXPOSE 8080

ENTRYPOINT ["/usr/src/app/tool.py"]
