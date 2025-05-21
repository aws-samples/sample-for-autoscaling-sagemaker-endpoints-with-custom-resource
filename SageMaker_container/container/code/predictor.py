
import os
import time
import flask

prefix = "/opt/ml/"
model_path = os.path.join(prefix, "model")
EndpointName = os.environ.get("ENDPOINT_NAME", "default-endpoint-name")

class CPUWorkerService(object):
    """A simple CPU worker service that simulates a CPU-bound task. The service will do some CPU bound work and sleep for a
    certain amount of time to simulate the CPU-bound task, and then return the result."""
    @classmethod
    def predict(cls, input):
        """For the input, do the predictions and return them.
        Args:
            input (a comma seperated string): the input to the model. The first element is the latency
                and the second element is the CPU usage percentage.
        Returns:
            String with Endpointname identifier, latency and CPU usage percentage.
        """
        args = input.split(",")
        Latency = int(args[0])
        CPU_usage = int(args[1])
        n = 100
        sleep_latency = (100 - CPU_usage) / 100 * Latency / 1000 / n
        work_latency = CPU_usage / 100 * Latency / 1000 / n
        
        for i in range(n):
            startTime = time.time()
            while time.time() - startTime < work_latency:
                10 * 10
            time.sleep(sleep_latency)

        return f"Endpoint Name: {EndpointName}, {Latency}, {CPU_usage}"


# The flask app for serving predictions
app = flask.Flask(__name__)


@app.route("/ping", methods=["GET"])
def ping():
    """Determine if the container is working and healthy. In this sample container, we declare
    it healthy if we can load the model successfully."""
    health = True

    status = 200 if health else 404
    return flask.Response(response="\n", status=status, mimetype="application/json")


@app.route("/invocations", methods=["POST"])
def transformation():
    """Do an inference on a single batch of data. In this sample server, we take data as CSV, convert
    it to a pandas data frame for internal use and then convert the predictions back to CSV (which really
    just means one prediction per line, since there's a single column.
    """
    data = None

    # Convert from CSV to pandas
    if flask.request.content_type == "text/csv":
        data = flask.request.data.decode("utf-8")
    else:
        return flask.Response(
            response="This predictor only supports CSV data", status=415, mimetype="text/plain"
        )
    
    # Do the prediction
    predictions = CPUWorkerService.predict(data)

    return flask.Response(response=predictions , status=200, mimetype="text/csv")