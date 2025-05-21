import json
import boto3
import os
import logging
import traceback
from decimal import Decimal
from botocore.exceptions import ClientError
import time

class DecimalEncoder(json.JSONEncoder):
  def default(self, obj):
    if isinstance(obj, Decimal):
      return str(obj)
    return json.JSONEncoder.default(self, obj)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

class ConfigurationError(Exception):
    """Custom exception for load balancer errors"""
    def __init__(self, message, status_code=500, details=None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

class ASGClass:
    def __init__(self):
        self.debugOn = False
        self.dynamodb = boto3.resource('dynamodb')
        self.sagemaker = boto3.client('sagemaker')
        self.table = self.dynamodb.Table(os.environ.get('STATE_TABLE_NAME', 'EMLStack-Table'))
        self.config_id = os.environ.get('CONFIG_ID', 'server_config')
        self.dimensionId = ""
        self.bodyData = None
        self.previousState = None
        self.currentState = None

    def get_dimensionId(self, event):
        """Extract dimensionId from the API Gateway event"""
        path = event.get('path', '')
        
        # Extract from path parameters if available
        if 'pathParameters' in event and event['pathParameters'] and 'dimensionId' in event['pathParameters']:
            return event['pathParameters']['dimensionId']
        
        # Otherwise try to parse from the path
        parts = path.split('/')
        for i, part in enumerate(parts):
            if part == "scalableTargetDimensions" and i + 1 < len(parts):
                return parts[i + 1]
        
        # If we can't find it, return a default or raise an error
        return "default-dimension-id"

    def read_state(self):
        """Read state from DynamoDB"""
        try:
            response = self.table.get_item(Key={'id': self.dimensionId})
            if 'Item' in response:
                return response['Item']
            else:
                raise KeyError(f"'Item' key not found in response: f{response}")
        except Exception as e:
            logger.error(f"Error reading state for {self.dimensionId}: {str(e)}")
            # Return a default state in case of error
            return {}

    def write_state(self):
        """Write state to DynamoDB"""
        try:
            self.currentState['id'] = self.dimensionId  # Ensure dimensionId is in the state
            # add modified timestamp
            self.currentState['lastModified'] = int(Decimal(time.time()) * 1000)
            self.table.put_item(Item=self.currentState)
            return True
        except Exception as e:
            logger.error(f"Error writing state for {self.dimensionId}: {str(e)}")
            return False

    def get_server_config(self):
        try:
            response = self.table.get_item(
                Key={'id': self.config_id}
            )
            
            if 'Item' in response:
                config = response['Item']
                servers = config.get('servers', [])
                weights = [int(w) for w in config.get('weights', [0] * len(servers))]
                currentInstanceCount = [int(w) for w in config.get('current_instance_count', [0] * len(servers))]
                
                return servers, currentInstanceCount, weights
            else:
                raise ConfigurationError("Server configuration not found in database", 500)
                
        except ClientError as e:
            logger.error(f"DynamoDB error retrieving server config: {str(e)}")
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            raise ConfigurationError(
                f"Error retrieving server configuration: {error_message}",
                500,
                {"error_code": error_code}
            )
    
    def update_endpoint(self, endpoint_name, variant_name, desired_instance_count):
        """Update the desired instance count for an endpoint"""
        try:
             self.sagemaker.update_endpoint_weights_and_capacities(
                EndpointName=endpoint_name,
                DesiredWeightsAndCapacities=[{
                    'VariantName': variant_name,
                    'DesiredInstanceCount': desired_instance_count
                }])
        except ClientError as e:
            logger.error(f"SageMaker error updating endpoint: {str(e)}")
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            raise ConfigurationError(
                f"Error updating endpoint: {error_message}",
                500,
                {"error_code": error_code}
            )
    
    def update_scaling(self):
        servers, currentInstanceCount, weights = self.get_server_config()
        delta = Decimal(self.currentState["desiredCapacity"]) - Decimal(self.currentState["actualCapacity"])
        if delta > 0:
            if sum(currentInstanceCount) == 0:
                self.update_endpoint(self.currentState["resourceName"] + '-' + servers[weights.index(1)], self.currentState["variantName"], int(delta))
            else:
                for i, server in enumerate(servers):
                    #do something
                    self.update_endpoint(self.currentState["resourceName"] + '-' + server, self.currentState["variantName"], int(currentInstanceCount[i] + delta))
        elif delta < 0:
            for i, server in enumerate(servers):
                if currentInstanceCount[i] >  -delta:
                    self.update_endpoint(self.currentState["resourceName"] + '-' + server, self.currentState["variantName"], int(currentInstanceCount[i] + delta))
                    break
                elif currentInstanceCount[i] > 0:
                    self.update_endpoint(self.currentState["resourceName"] + '-' + server, self.currentState["variantName"], 0)
                    delta += currentInstanceCount[i]


    def patch_state(self):
        """Update state with values from the request body"""
        if "actualCapacity" in self.bodyData:
            self.currentState["actualCapacity"] = Decimal(self.bodyData["actualCapacity"])
        if "desiredCapacity" in self.bodyData and self.currentState["scalingStatus"] not in ["InProgress", "Pending"]:
            self.currentState["desiredCapacity"] = Decimal(self.bodyData["desiredCapacity"])
            if self.currentState["desiredCapacity"] > 0:
                self.currentState["scalingStatus"] = "Pending"
                self.update_scaling()
        if "scalingStatus" in self.bodyData:
            self.currentState["scalingStatus"] = self.bodyData["scalingStatus"]

    def log_state_changes(self, http_method):
        """Log state changes to CloudWatch"""
        log_response = ""
        if self.previousState["desiredCapacity"] != self.currentState["desiredCapacity"]:
            log_response += f"desiredCapacity: {self.previousState['desiredCapacity']} -> {self.currentState['desiredCapacity']}  "
        if self.previousState["actualCapacity"] != self.currentState["actualCapacity"]:
            log_response += f"actualCapacity: {self.previousState['actualCapacity']} -> {self.currentState['actualCapacity']}  "
        if self.previousState["scalingStatus"] != self.currentState["scalingStatus"]:
            log_response += f"scalingStatus: {self.previousState['scalingStatus']} -> {self.currentState['scalingStatus']}"
        
        if not log_response:
            log_response += f"actualCapacity: {self.currentState['actualCapacity']} = desiredCapacity: {self.currentState['desiredCapacity']}  "
        
        body_out = json.dumps(self.bodyData) if self.bodyData else ""
        
        logger.info(f"Request: {http_method} dimensionId: {self.dimensionId} {body_out}")
        logger.info(f"Response: dimensionId: {self.dimensionId} {log_response}")
        
        if self.debugOn:
            logger.debug(f"Debug info for dimensionId: {self.dimensionId}")
            logger.debug(f"Request body: {body_out}")

def lambda_handler(event, context):
    """Main Lambda handler function"""
    try:

        # Initialize ASGClass
        asg = ASGClass()
        # Extract HTTP method
        http_method = event.get('httpMethod', 'GET')
        
        # Get dimension ID from the request
        asg.dimensionId = asg.get_dimensionId(event)
        
        # Parse request body if present
        if 'body' in event and event['body']:
            try:
                asg.bodyData = json.loads(event['body'])
            except:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': 'Invalid JSON in request body'})
                }
        
        # Read current state
        asg.currentState = asg.read_state()
        asg.previousState = dict(asg.currentState)
        asg.dimensionId = asg.currentState["scalableTargetDimensionId"]
        
        # Process based on HTTP method
        if asg.bodyData and http_method == 'PATCH':
            asg.patch_state()
        
        elif http_method != 'GET':
            return {
                'statusCode': 405,
                'body': json.dumps({'error': 'Method Not Allowed'})
            }
        else:
            if asg.currentState["desiredCapacity"] >= 0:
                if (asg.currentState["scalingStatus"] == "Successful" or asg.currentState["scalingStatus"] == "Failed") and asg.currentState["desiredCapacity"] != asg.currentState["actualCapacity"]:
                    asg.currentState["scalingStatus"] = "Pending"
                    asg.update_scaling()
                if asg.currentState["lastModified"] < int(Decimal(time.time()) * 1000) - 25 * 60 * 1000 and (asg.currentState["scalingStatus"] == "Pending" or asg.currentState["scalingStatus"] == "InProgress"):
                    if asg.currentState["desiredCapacity"] != asg.currentState["actualCapacity"]:
                        asg.currentState["scalingStatus"] = "Failed"
                        asg.currentState["failureResaon"] = "Scaling activity stayed in Pending or InProgress for more than 25 minutes"
                    else:
                        asg.currentState["scalingStatus"] = "Successful"

        if asg.currentState["scalingStatus"] != "Failed":
            asg.currentState["failureResaon"] = ""
        # Log state changes
        asg.log_state_changes(http_method)
        returningJson = {
            "actualCapacity": float(asg.currentState["actualCapacity"]),
            "desiredCapacity": float(asg.currentState["desiredCapacity"]),
            "dimensionName": asg.currentState["dimensionName"],
            "resourceName": asg.currentState["resourceName"],
            "scalableTargetDimensionId": asg.currentState["scalableTargetDimensionId"],
            "scalingStatus": asg.currentState["scalingStatus"],
            "version": asg.currentState["version"]
        }
        asg.write_state()
        # Return response
        return {
            "statusCode": "200",
            "body": json.dumps(returningJson)
        }
    
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}, stacktrace: {traceback.format_exc()}")
        return {
            'statusCode': 500,
            'body': json.dumps({'event': event, 'error': f'Internal server error: {str(e)}, "stacktrace": "{traceback.format_exc()}"'})
        }