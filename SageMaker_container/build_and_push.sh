#!/bin/bash

# Script to build a Docker image and push it to Amazon ECR
# Usage: ./build_and_push.sh [image-name] [aws-region]

set -e  # Exit immediately if a command exits with a non-zero status

# Default values
IMAGE_NAME=${1:-sagemaker-container}
AWS_REGION=${2:-us-east-1}
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPOSITORY_NAME=${IMAGE_NAME}
ECR_IMAGE_TAG="latest"
FULL_IMAGE_NAME="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY_NAME}:${ECR_IMAGE_TAG}"

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo "AWS CLI is not installed. Please install it first."
    exit 1
fi

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Please install it first."
    exit 1
fi

# Print information
echo "=== Building and pushing Docker image to Amazon ECR ==="
echo "Image Name: ${IMAGE_NAME}"
echo "AWS Region: ${AWS_REGION}"
echo "AWS Account ID: ${AWS_ACCOUNT_ID}"
echo "ECR Repository: ${ECR_REPOSITORY_NAME}"
echo "Image Tag: ${ECR_IMAGE_TAG}"
echo "Full Image Name: ${FULL_IMAGE_NAME}"
echo "=================================================="

# Build the Docker image
echo "Building Docker image..."
docker buildx build --platform linux/amd64 -t ${IMAGE_NAME}:${ECR_IMAGE_TAG} -f container/Dockerfile container/

# Authenticate Docker to ECR
echo "Authenticating with Amazon ECR..."
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Create ECR repository if it doesn't exist
echo "Checking if ECR repository exists..."
if ! aws ecr describe-repositories --repository-names ${ECR_REPOSITORY_NAME} --region ${AWS_REGION} &> /dev/null; then
    echo "Creating ECR repository: ${ECR_REPOSITORY_NAME}"
    aws ecr create-repository --repository-name ${ECR_REPOSITORY_NAME} --region ${AWS_REGION}
fi

# Tag the image for ECR
echo "Tagging image for ECR..."
docker tag ${IMAGE_NAME}:${ECR_IMAGE_TAG} ${FULL_IMAGE_NAME}

# Push the image to ECR
echo "Pushing image to ECR..."
docker push ${FULL_IMAGE_NAME}

echo "=== Successfully built and pushed image to Amazon ECR ==="
echo "Image URI: ${FULL_IMAGE_NAME}"
