import boto3
import json
import os
import time
import uuid
import logging
from decimal import Decimal
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')
sagemaker = boto3.client('sagemaker-runtime')

# Get table name from environment variable or use default
LOAD_BALANCER_TABLE = os.environ.get('DYNAMODB_TABLE')

allowed_headers = [
    "Accept",
    "ContentType",
    "CustomAttributes",
    "InferenceId",
    "InputLocation",
    "InvocationTimeoutSeconds",
    "RequestTTLSeconds"
]

required_headers = [
    "InputLocation"
]

class LoadBalancerError(Exception):
    """Custom exception for load balancer errors"""
    def __init__(self, message, status_code=500, details=None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class WeightedLoadBalancer:
    def __init__(self):
        """Initialize the load balancer with DynamoDB connection"""
        self.table = dynamodb.Table(LOAD_BALANCER_TABLE)
        self.counter_id = os.environ.get('COUNTER_ID', 'request_counter')
        self.config_id = os.environ.get('CONFIG_ID', 'server_config')
    
    def get_server_config(self):
        """
        Retrieve server configuration from DynamoDB
        
        Returns:
            tuple: (servers, weights) lists
            
        Raises:
            LoadBalancerError: If there's an issue retrieving the configuration
        """
        try:
            response = self.table.get_item(
                Key={'id': self.config_id}
            )
            
            if 'Item' in response:
                config = response['Item']
                servers = config.get('servers', [])
                weights = [int(w) for w in config.get('weights', [])]
                
                if not servers or not weights or len(servers) != len(weights):
                    raise LoadBalancerError(
                        "Invalid server configuration in database", 
                        500, 
                        {"servers": servers, "weights": weights}
                    )
                
                return servers, weights
            else:
                # Default configuration if none exists
                default_servers = ["server1", "server2", "server3"]
                default_weights = [5, 3, 2]
                
                # Store default config
                try:
                    self.table.put_item(
                        Item={
                            'id': self.config_id,
                            'servers': default_servers,
                            'weights': [Decimal(str(w)) for w in default_weights],
                            'current_instance_count': [Decimal(str(w)) for w in default_weights]
                        }
                    )
                except ClientError as e:
                    logger.error(f"Failed to store default configuration: {str(e)}")
                    raise LoadBalancerError(
                        "Failed to initialize server configuration", 
                        500, 
                        {"error": str(e)}
                    )
                
                return default_servers, default_weights
                
        except ClientError as e:
            logger.error(f"DynamoDB error retrieving server config: {str(e)}")
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            
            raise LoadBalancerError(
                f"DynamoDB error: {error_code}", 
                500, 
                {"error_code": error_code, "error_message": error_message}
            )
        except Exception as e:
            logger.error(f"Unexpected error retrieving server config: {str(e)}")
            raise LoadBalancerError(
                "Failed to retrieve server configuration", 
                500, 
                {"error": str(e)}
            )
    
    def get_and_increment_counter(self):
        """
        Get the current counter value and increment it atomically
        
        Returns:
            int: The counter value before incrementing
            
        Raises:
            LoadBalancerError: If there's an issue with the counter operation
        """
        try:
            # Use atomic counter update
            response = self.table.update_item(
                Key={'id': self.counter_id},
                UpdateExpression='SET request_count = if_not_exists(request_count, :start) + :inc, #type = if_not_exists(#type, :type_val)',
                ExpressionAttributeNames={
                    '#type': 'type'  # 'type' is a reserved word in DynamoDB
                },
                ExpressionAttributeValues={
                    ':inc': Decimal('1'),
                    ':start': Decimal('0'),
                    ':type_val': 'counter'
                },
                ReturnValues='UPDATED_OLD'
            )
            
            # Get the previous value (before increment)
            if 'Attributes' in response and 'request_count' in response['Attributes']:
                return int(response['Attributes']['request_count'])
            return 0
            
        except ClientError as e:
            logger.error(f"DynamoDB error updating counter: {str(e)}")
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            
            # Generate a fallback counter value
            fallback_count = int(time.time() * 1000) % 1000
            logger.info(f"Using fallback counter value: {fallback_count}")
            
            raise LoadBalancerError(
                f"Counter update failed: {error_code}", 
                500, 
                {
                    "error_code": error_code, 
                    "error_message": error_message,
                    "fallback_count": fallback_count
                }
            )
        except Exception as e:
            logger.error(f"Unexpected error updating counter: {str(e)}")
            
            # Generate a fallback counter value
            fallback_count = int(time.time() * 1000) % 1000
            logger.info(f"Using fallback counter value: {fallback_count}")
            
            raise LoadBalancerError(
                "Failed to update request counter", 
                500, 
                {"error": str(e), "fallback_count": fallback_count}
            )
    
    def get_next_server(self):
        """
        Get the next server based on the current request count and weights
        
        Returns:
            dict: Information about the selected server
            
        Raises:
            LoadBalancerError: If there's an issue determining the next server
        """
        try:
            # Get server configuration
            servers, weights = self.get_server_config()
            
            # Get and increment the counter
            try:
                request_count = self.get_and_increment_counter()
            except LoadBalancerError as e:
                # Use fallback counter if available
                if 'fallback_count' in e.details:
                    request_count = e.details['fallback_count']
                    logger.info(f"Using fallback counter: {request_count}")
                else:
                    raise
            
            # Calculate total weight
            total_weight = sum(weights)
            if total_weight <= 0:
                raise LoadBalancerError(
                    "Invalid server weights configuration", 
                    500, 
                    {"weights": weights, "total_weight": total_weight}
                )
            
            # Calculate position within the weight cycle
            position = (request_count % total_weight) + 1
            
            # Find which server's weight range contains this position
            cumulative = 0
            for i, weight in enumerate(weights):
                cumulative += weight
                if position <= cumulative:
                    return {
                        "server": servers[i],
                        "request_id": str(uuid.uuid4()),
                        "request_count": request_count + 1  # +1 because we're returning the new count
                    }
            
            # Fallback (should not reach here if implementation is correct)
            logger.warning("Server selection algorithm reached unexpected fallback path")
            return {
                "server": servers[0],
                "request_id": str(uuid.uuid4()),
                "request_count": request_count + 1,
                "warning": "Used fallback server selection"
            }
            
        except LoadBalancerError:
            # Re-raise LoadBalancerError exceptions
            raise
        except Exception as e:
            logger.error(f"Unexpected error in get_next_server: {str(e)}")
            raise LoadBalancerError(
                "Failed to determine next server", 
                500, 
                {"error": str(e)}
            )
    
    def update_server_config(self, servers, weights):
        """
        Update the server configuration in DynamoDB
        
        Args:
            servers (list): List of server identifiers
            weights (list): List of weights corresponding to each server
            
        Returns:
            dict: Updated configuration
            
        Raises:
            LoadBalancerError: If there's an issue updating the configuration
        """
        if not servers or not weights:
            raise LoadBalancerError(
                "Missing servers or weights", 
                400, 
                {"servers": servers, "weights": weights}
            )
            
        if len(servers) != len(weights):
            raise LoadBalancerError(
                "Number of servers must match number of weights", 
                400, 
                {"servers_count": len(servers), "weights_count": len(weights)}
            )
        
        # Validate weights are positive integers
        for i, weight in enumerate(weights):
            try:
                weight_val = int(weight)
                if weight_val < 0:
                    raise LoadBalancerError(
                        f"Weight for server {servers[i]} must be non-negative", 
                        400, 
                        {"server": servers[i], "weight": weight}
                    )
                weights[i] = weight_val
            except ValueError:
                raise LoadBalancerError(
                    f"Weight for server {servers[i]} must be a number", 
                    400, 
                    {"server": servers[i], "weight": weight}
                )
            
        try:
            timestamp = Decimal(str(time.time()))
            item = {
                'id': self.config_id,
                'type': 'config',
                'servers': servers,
                'weights': [Decimal(str(w)) for w in weights],
                'updated_at': timestamp
            }
            
            self.table.put_item(Item=item)
            
            # Return the updated configuration (without Decimal types)
            return {
                'servers': servers,
                'weights': weights,
                'updated_at': float(timestamp)
            }
            
        except ClientError as e:
            logger.error(f"DynamoDB error updating server config: {str(e)}")
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            
            raise LoadBalancerError(
                f"Failed to update configuration: {error_code}", 
                500, 
                {"error_code": error_code, "error_message": error_message}
            )
        except Exception as e:
            logger.error(f"Unexpected error updating server config: {str(e)}")
            raise LoadBalancerError(
                "Failed to update server configuration", 
                500, 
                {"error": str(e)}
            )


# Lambda handler function
def handler(event, context):
    """
    AWS Lambda handler function
    
    Args:
        event: Lambda event data
        context: Lambda context
    
    Returns:
        dict: Response with selected server information or error details
    """
    if event.get("httpMethod") == "POST":
        headers = {}
        try:
            EndpointName = event["pathParameters"]["EndpointName"]
            for required_header in required_headers:
                if required_header not in event["headers"]: # 
                    raise LoadBalancerError(
                        f"Missing required header: {required_header}",
                        400,
                        {"event": event}
                    )
            
            for allowed_header in allowed_headers:
                if allowed_header in event["headers"]: #
                    headers[allowed_header] = event["headers"][allowed_header] #

            lb = WeightedLoadBalancer()
            
            # Log the incoming event
            logger.info(f"Received event: {json.dumps(event)}")
                        
            # Default behavior: get next server
            result = lb.get_next_server()

            headers["EndpointName"] = f"{EndpointName}-{result['server']}"

            response = sagemaker.invoke_endpoint_async(**headers)
            response["input_headers"] = headers
            
            return {
                'statusCode': 200,
                'body': json.dumps(response),
                'headers': {
                    'Content-Type': 'application/json'
                }
            }
            
        except LoadBalancerError as e:
            logger.error(f"LoadBalancerError: {e.message} - {json.dumps(e.details)}")
            return {
                'statusCode': e.status_code,
                'body': json.dumps({
                    'error': e.message,
                    'details': e.details,
                    'event': event
                }),
                'headers': {'Content-Type': 'application/json'}
            }
        except Exception as e:
            logger.error(f"Unhandled exception: {str(e)}")
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'error': 'Internal server error',
                    'message': str(e),
                    'event': event
                }),
                'headers': {'Content-Type': 'application/json'}
            }
    else:
        return {
            'statusCode': 200,
            'body': json.dumps({'event': event}),
            'headers': {'Content-Type': 'application/json'}
        }