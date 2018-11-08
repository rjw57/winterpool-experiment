# We need to start from a recent ubuntu to make sure we have the latest
# tesseract OCR package.
FROM ubuntu:bionic
WORKDIR /usr/src/app

# Make Python use Unicode
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV LANGUAGE=C.UTF-8
ENV PYTHONIOENCODING=utf-8

ADD requirements.txt /usr/src/app
RUN apt-get -y update \
    && apt-get -y install swig build-essential libjpeg-turbo8-dev \
        libpulse-dev tesseract-ocr texlive-latex-recommended poppler-utils \
        python3 python3-dev python3-pip python3-lxml \
    && pip3 install -r requirements.txt \
    && apt-get -y clean

ADD tool.py /usr/src/app
ADD templates/ /usr/src/app/templates
VOLUME /usr/src/app/store

# For local OAuth2 auth webserver
EXPOSE 8080

ENTRYPOINT ["/usr/src/app/tool.py"]
