import boto3
import requests
from aws_requests_auth.aws_auth import AWSRequestsAuth
import json

router_api_gateway_id = "<router-api-gatway-id>"
router_api_gateway_stage = "prod"
aws_region = boto3.Session().region_name
input_location = "s3://<your-s3-bucket and prefix>/payload.csv"
router_api_gateway_host = f"{router_api_gateway_id}.execute-api.{aws_region}.amazonaws.com"
router_api_gateway_endpoint = f"https://{router_api_gateway_id}.execute-api.{aws_region}.amazonaws.com/{router_api_gateway_stage}"

session = boto3.Session()
credentials = session.get_credentials()

auth = AWSRequestsAuth(aws_access_key=credentials.access_key,
                       aws_secret_access_key=credentials.secret_key,
                       aws_token=credentials.token,
                       aws_host=router_api_gateway_host,
                       aws_region=aws_region,
                       aws_service="execute-api")

headers = {
    "ContentType": "text/csv",
    "InputLocation": input_location
}


response = requests.post(f"{router_api_gateway_endpoint}/endpoints/endpoint", auth=auth, headers=headers)

print(json.dumps(json.loads(response.content), indent=4))
