import json
import boto3
import logging
import os
from decimal import Decimal
import traceback
from botocore.exceptions import ClientError
from math import gcd
import time

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
        self.dynamodb = boto3.resource('dynamodb')
        self.sagemaker = boto3.client('sagemaker')
        self.dimensionId = os.environ.get('DIMENSION_ID', 'custom-resource:ResourceType:Property')
        self.table = self.dynamodb.Table(os.environ.get('STATE_TABLE_NAME'))
        self.config_id = os.environ.get('CONFIG_ID', 'server_config')
        self.config = None
        self.currentState = {}
        self.servers = []
        self.weights = []
        self.currentInstanceCount = []

    def get_server_config(self):
        try:
            response = self.table.get_item(
                Key={'id': self.config_id}
            )
            
            if 'Item' in response:
                self.config = response['Item']
                self.servers = self.config.get('servers', [])
                self.weights = [int(w) for w in self.config.get('weights', [0] * len(self.servers))]
                self.currentInstanceCount = [int(w) for w in self.config.get('current_instance_count', [0] * len(self.servers))]

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

    def update_server_config(self):
        try:
            item = {
                'id': self.config_id,
                'servers': self.servers,
                'weights': [Decimal(str(w)) for w in self.weights],
                "current_instance_count": [Decimal(str(c)) for c in self.currentInstanceCount]
            }
            
            self.table.put_item(Item=item)
            
        except ClientError as e:
            logger.error(f"DynamoDB error updating server config: {str(e)}")
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            
            raise ConfigurationError(
                f"Failed to update configuration: {error_code}", 
                500, 
                {"error_code": error_code, "error_message": error_message}
            )
        except Exception as e:
            logger.error(f"Unexpected error updating server config: {str(e)}")
            raise ConfigurationError(
                "Failed to update server configuration", 
                500, 
                {"error": str(e)}
            )

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
            self.currentState['lastModified'] = int(Decimal(time.time()) * 1000)
            self.table.put_item(Item=self.currentState)
            return True
        except Exception as e:
            logger.error(f"Error writing state for {self.dimensionId}: {str(e)}")
            return False

def lambda_handler(event, context):
    """
    Lambda function to handle EventBridge events for SageMaker endpoint status changes.
    
    Parameters:
    event (dict): The EventBridge event
    context (LambdaContext): Lambda context object
    
    Returns:
    dict: Response indicating success or failure
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    try:
        result = handle_endpoint_status_change(event)
        
        return result
    
    except Exception as e:
        logger.error(f"Error processing event: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': f'Internal server error: {str(e)}, "stacktrace": "{traceback.format_exc()}"'})
        }

def handle_endpoint_status_change(event):
    """
    Handle SageMaker endpoint status change events
    
    Parameters:
    event (dict): The EventBridge event for SageMaker endpoint status change
    
    Returns:
    dict: Response indicating success or failure
    """
    try:
        asg = ASGClass()
        asg.get_server_config()
        asg.currentState = asg.read_state()
        detail = event.get('detail', {})
        endpoint_name = detail.get('EndpointName')
        endpoint_status = detail.get('EndpointStatus')
        if asg.currentState.get("resourceName") in endpoint_name:
            logger.info(f"Processing endpoint status change for {endpoint_name}: {endpoint_status}")
            
            # Handle different endpoint statuses
            if endpoint_status == 'IN_SERVICE':
                server = endpoint_name.split("-")[1]
                index = asg.servers.index(server)
                asg.currentInstanceCount[index] = detail.get("ProductionVariants")[0].get("DesiredInstanceCount")
                asg.currentState["actualCapacity"] = Decimal(sum(asg.currentInstanceCount))
                if asg.currentState["desiredCapacity"] < asg.currentState["actualCapacity"] and asg.currentInstanceCount[index] > 0:
                    asg.sagemaker.update_endpoint_weights_and_capacities(
                            EndpointName=endpoint_name,
                            DesiredWeightsAndCapacities=[{
                                'VariantName': asg.currentState["variantName"],
                                'DesiredInstanceCount': int(asg.currentInstanceCount[index] - 1)
                            }])
                    asg.currentState["scalingStatus"] = "InProgress"
                elif asg.currentState["desiredCapacity"] == asg.currentState["actualCapacity"]:
                    gcd_value =  gcd(*asg.currentInstanceCount)
                    if gcd_value != 0:
                        for i in range(len(asg.weights)):
                            asg.weights[i] = int(asg.currentInstanceCount[i] / gcd_value)
                    if sum(asg.weights) == 0:
                        asg.weights[0] = 1
                    asg.currentState["scalingStatus"] = "Successful"
                else:
                    asg.currentState["scalingStatus"] = "InProgress"

                asg.update_server_config()
                asg.write_state()
                
            elif endpoint_status == 'FAILED':
                # Endpoint deployment failed
                failure_reason = detail.get('FailureReason', 'Unknown reason')
                logger.error(f"Endpoint {endpoint_name} failed: {failure_reason}")
                
                asg.currentState["scalingStatus"] = "Failed"
                asg.write_state()
                
            elif endpoint_status == 'CREATING':
                # Endpoint is being created
                asg.currentState["scalingStatus"] = "Pending"
                asg.write_state()
                
            elif endpoint_status == 'UPDATING':
                # Endpoint is being updated
                asg.currentState["scalingStatus"] = "InProgress"
                asg.write_state()
                
            elif endpoint_status == 'SYSTEM_UPDATING':
                # Endpoint is undergoing system updates
                asg.currentState["scalingStatus"] = "Pending"
                asg.write_state()
                
            elif endpoint_status == 'ROLLING_BACK':
                # Endpoint is rolling back to a previous configuration
                asg.currentState["scalingStatus"] = "Pending"
                asg.write_state()
                
            elif endpoint_status == 'OUT_OF_SERVICE':
                # Endpoint is out of service
                asg.currentState["scalingStatus"] = "Failed"
                asg.write_state()
                
            else:
                logger.info(f"Unhandled endpoint status: {endpoint_status}")
                # TODO: Add your custom logic for other endpoint statuses
                pass
    except Exception as e:
        logger.error(f"Error processing endpoint status change for {endpoint_name}: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': f'Internal server error: {str(e)}, "stacktrace": "{traceback.format_exc()}"'})
        }
    
    return {
        'statusCode': 200,
        'body': json.dumps(f'Successfully processed endpoint status change for {endpoint_name}')
    }
