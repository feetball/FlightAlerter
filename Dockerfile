
FROM amancevice/pandas:latest
#FROM alpine:latest
ADD repositories /etc/apk/repositories
WORKDIR /
#RUN apk update
#RUN apk add make automake gcc g++ subversion python3-dev
#RUN apk add --no-cache gcc musl-dev linux-headers

ENV PYTHONUNBUFFERED=1
#RUN apk add --update --no-cache python3 && ln -sf python3 /usr/bin/python

RUN python3 -m ensurepip
RUN pip3 install --no-cache --upgrade pip setuptools

RUN pip3 install --no-cache-dir numpy

COPY requirements.txt requirements.txt
COPY monitoredFlights.csv monitoredFlights.csv
COPY sentAlerts.csv sentAlerts.csv
#COPY cronFlightAlerter /etc/cron.d/cronFlightAlerter
#RUN chmod 0644 /etc/cron.d/cronFlightAlerter
#RUN crontab /etc/cron.d/cronFlightAlerter
#RUN touch /var/log/cron.log
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "/flightAlerter.py"]
#CMD ['crond', '-l 2', '-f']
