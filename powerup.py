#!/usr/bin/env python

import os
import sys
import base64
import hashlib
import yaml
import boto3
import botocore





from troposphere import Base64, Select, FindInMap, GetAtt, GetAZs, Join, Output, If, And, Not, Or, Equals, Condition
from troposphere import Parameter, Ref, Tags, Template, ImportValue
from troposphere.cloudformation import Init
from troposphere.cloudfront import Distribution, DistributionConfig
from troposphere.cloudfront import Origin, DefaultCacheBehavior
from troposphere.ec2 import PortRange
from troposphere.apigateway import Deployment
from troposphere.apigateway import Method
from troposphere.apigateway import Resource, MethodResponse, Integration, IntegrationResponse
from troposphere.awslambda import Version, Permission, Function, Code, DeadLetterConfig, VPCConfig, Environment
from troposphere.logs import LogGroup, SubscriptionFilter
import troposphere.sqs as sqs
import time

def generate_deployment():
    t = Template()

    t.add_version("2010-09-09")

    t.add_description("""API Gateway Deployment""")

    EnvironmentName = t.add_parameter(Parameter(
        "EnvironmentName",
        Type="String",
    ))

    ApiGatewayDeployment = t.add_resource(Deployment(
        "ApiGatewayDeployment{0}".format(int(time.time())),
        RestApiId=ImportValue(Join("-", [Ref(EnvironmentName), "RestApi"])),
        StageName="dev",
    ))

    return t.to_json()

def generate(service_definition):
    t = Template()

    t.add_version("2010-09-09")

    t.add_description("""API Gateway Service""")

    EnvironmentName = t.add_parameter(Parameter(
        "EnvironmentName",
        Type="String",
    ))

    ServiceName = t.add_parameter(Parameter(
        "ServiceName",
        Type="String",
    ))

    LambdaPackageShaSum = t.add_parameter(Parameter(
        "LambdaPackageShaSum",
        Type="String",
    ))

    LambdaPackageS3Key = t.add_parameter(Parameter(
        "LambdaPackageS3Key",
        Type="String",
    ))

    

    service_root_path = service_definition['rootpath']

    ServiceRootResource = t.add_resource(Resource(
        "{0}Resource".format(service_root_path.capitalize()),
        RestApiId=ImportValue(Join("-", [Ref(EnvironmentName), "RestApi"])),
        PathPart=service_root_path,
        ParentId=ImportValue(Join("-", [Ref(EnvironmentName), "RootResource"])),
    ))

    for function in service_definition['functions']:
        function_name = function
        function_handler = 'handler.Handle'#service_definition['functions'][function]['handler']
        function_path = '{method+}'#service_definition['functions'][function]['path']
        
        # Create SQS Queue
        queue = t.add_resource(
            sqs.Queue(
                "{0}LambdaQueue".format(function_name.capitalize()),
                QueueName="{0}LambdaQueue".format(function_name.capitalize()),
                DelaySeconds=0,
                ReceiveMessageWaitTimeSeconds=20,
                MaximumMessageSize=262144,
                VisibilityTimeout=30,
                MessageRetentionPeriod=1209600
            )
        )

        FunctionResource = t.add_resource(Resource(
            "{0}FunctionResource".format(function_name.capitalize()),
            RestApiId=ImportValue(Join("-", [Ref(EnvironmentName), "RestApi"])),
            PathPart=function_path,
            ParentId=Ref(ServiceRootResource),
        ))

        LambdaPermission = t.add_resource(Permission(
            "{0}LambdaPermission".format(function_name.capitalize()),
            Action="lambda:InvokeFunction",
            FunctionName=GetAtt("{0}LambdaFunction".format(function_name.capitalize()), "Arn"),
            SourceArn=ImportValue(Join("-", [Ref(EnvironmentName), "GatewayArn"])),
            Principal="apigateway.amazonaws.com",
        ))

        LambdaFunction = Function(
            "{0}LambdaFunction".format(function_name.capitalize()),
            #TracingConfig={ "Mode": "Active" },
            Code=Code(
                S3Bucket=ImportValue(Join("-", [Ref(EnvironmentName), "LambdaCodeBucket"])),
                S3Key=Ref(LambdaPackageS3Key)
            ),
            DeadLetterConfig=DeadLetterConfig(
                    TargetArn=GetAtt(queue, "Arn")
                ),
            VpcConfig=VPCConfig(
                SubnetIds=[
                    ImportValue(Join("-", [Ref(EnvironmentName), "PrivateSubnet1"])),
                    ImportValue(Join("-", [Ref(EnvironmentName), "PrivateSubnet2"]))
                ],
                SecurityGroupIds=[
                    ImportValue(Join("-", [Ref(EnvironmentName), "LambdaSecurityGroup"]))
                ]
            ),
            MemorySize=1536,
            Handler=function_handler,
            Role=ImportValue(Join("-", [Ref(EnvironmentName), "LambdaExecutionRoleArn"])),
            Timeout=300,
            Runtime="python2.7",
        )

        try:
            function_vars = service_definition['functions'][function]['env']
            LambdaFunction.Environment = Environment(Variables=function_vars)
        except KeyError as e:
            print("No environment variables defined in service template")

        t.add_resource(LambdaFunction)

        FunctionLogGroup = t.add_resource(LogGroup(
            "{0}FunctionLogGroup".format(function_name.capitalize()),
            LogGroupName=Join("", ["/aws/lambda/", Ref(LambdaFunction)]),
            RetentionInDays=7
        ))

        LogglySubscriptionFilter = t.add_resource(SubscriptionFilter(
            "{0}LogglySubscriptionFilter".format(function_name.capitalize()),
            DependsOn=[ "{0}CloudwatchLogsInvokePermission".format(function_name.capitalize()) ],
            DestinationArn=ImportValue("Account-LogglyShipperLambdaArn"),
            FilterPattern="",
            LogGroupName=Ref(FunctionLogGroup)
        ))

        CloudwatchLogsInvokePermission = t.add_resource(Permission(
            "{0}CloudwatchLogsInvokePermission".format(function_name.capitalize()),
            FunctionName=ImportValue("Account-LogglyShipperLambdaArn"),
            Action="lambda:InvokeFunction",
            SourceArn=GetAtt(FunctionLogGroup, "Arn"),
            Principal="logs.us-west-2.amazonaws.com",
            SourceAccount=Ref("AWS::AccountId")
        ))

        CorsMethods = t.add_resource(Method(
            "{0}CorsMethod".format(function_name.capitalize()),
            RestApiId=ImportValue(Join("-", [Ref(EnvironmentName), "RestApi"])),
            MethodResponses=[
                MethodResponse(
                    StatusCode='200',
                    ResponseModels={},
                    ResponseParameters={
                        "method.response.header.Access-Control-Allow-Origin": True,
                        "method.response.header.Access-Control-Allow-Headers": True,
                        "method.response.header.Access-Control-Allow-Methods": True,
                        "method.response.header.Access-Control-Allow-Credentials": True
                    }
                )
            ],
            RequestParameters={},
            ResourceId=Ref(FunctionResource),
            AuthorizationType="NONE",
            HttpMethod="OPTIONS",
            Integration=Integration(
                RequestTemplates={ "application/json": "{statusCode:200}" },
                Type="MOCK",
                IntegrationResponses=[
                    IntegrationResponse(
                        ResponseParameters={
                            "method.response.header.Access-Control-Allow-Credentials": "'false'",
                            "method.response.header.Access-Control-Allow-Origin": "'*'",
                            "method.response.header.Access-Control-Allow-Methods": "'OPTIONS,POST'",
                            "method.response.header.Access-Control-Allow-Headers": "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token,X-Amz-User-Agent'"
                        },
                        ResponseTemplates={ "application/json": "" },
                        StatusCode="200"
                    )
                ],
            ),
        ))


        FunctionMethod = t.add_resource(Method(
            "{0}FunctionMethod".format(function_name.capitalize()),
            RestApiId=ImportValue(Join("-", [Ref(EnvironmentName), "RestApi"])),
            MethodResponses=[],
            RequestParameters={},
            ResourceId=Ref(FunctionResource),
            AuthorizationType="NONE",
            Integration=Integration(
                IntegrationHttpMethod="POST",
                RequestTemplates={ "application/json": "{statusCode:200}" },
                Type="AWS_PROXY",
                Uri=Join("", ["arn:aws:apigateway:", Ref("AWS::Region"), ":lambda:path/2015-03-31/functions/", GetAtt(LambdaFunction, "Arn"), "/invocations"])
            ),
            HttpMethod="POST",
        ))


    return t.to_json()





def get_bucket_name(environment):
  stack_name = '{0}-Gateway'.format(environment.capitalize())
  client = boto3.client('cloudformation')
  try:
    stacks = client.describe_stacks(StackName = stack_name)['Stacks']
    if stacks:
      for output in stacks[0]['Outputs']:
        if output["OutputKey"] == "LambdaCodeBucketName":
          return output["OutputValue"]
    else:
      print("Could not find stack: {0}".format(stack_name))
  except botocore.exceptions.ClientError as e:
    print(e)

def get_package_checksum(filename):
    with open(filename, 'rb') as f:
        m = hashlib.sha256()
        m.update(f.read())
        hex_checksum = m.hexdigest()
        sha = m.digest()
        base64_checksum = base64.b64encode(sha)
        return base64_checksum, hex_checksum

def check_package_exists(bucketname, key):
  try:
    s3 = boto3.client('s3')
    s3.head_object(Bucket=bucketname, Key=key)
    return True
  except botocore.exceptions.ClientError:
    return False

def upload_package(bucketname, filename, key):
  print(bucketname)
  print(filename)
  print(key)
  s3 = boto3.resource('s3')
  s3.Bucket(bucketname).upload_file(filename, key)

def parse_yaml(filename):
  with open(filename, 'r') as f:
      return yaml.load(f)

# The API Gateway deployment has to be recreated anytime a resource is changed.
def update_deployment():
  print("Updating Deployment...")
  deployment_stack_name = "{0}-Deployment".format(environment.capitalize())
  client.update_stack(StackName=deployment_stack_name, TemplateBody=generate_deployment(), Capabilities=[ 'CAPABILITY_IAM' ],
      Parameters=[
      {
        'ParameterKey': 'EnvironmentName',
        'ParameterValue': str(environment)
      }
    ]
  )
  print("Done.")

service_yml_file = "service.yml"

# The package file is the compiled code. Powerup does not do the packaging.
package_file = "build/handler.zip"

print("\nStarting Powerup!(TM)\n")

cwd = os.getcwd()

if 'POWERUP_ENVIRONMENT' in os.environ:
    environment = os.environ["POWERUP_ENVIRONMENT"]
elif len(sys.argv) > 1:
    environment = sys.argv[1]
else:
    print('please set POWERUP_ENVIRONMENT in env or set deployment as arg 1')
    os._exit(0) 


print("Current working directory: {0}.".format(cwd))
print("Using environment: {0}.".format(environment))

# If no package file is present at the hardcoded location abort.
if os.path.isfile(package_file):
  print("Found package at {0}.".format(package_file))
  # base64 checksum is used for Lambda versioning. Hex checksum is used for S3 versioning.
  base64_checksum, hex_checksum = get_package_checksum(package_file)
  print("Package checksums: {0}, {1}".format(base64_checksum, hex_checksum))
  # The bucket to upload the package to. Name is defined as a Cloudformation stack output.
  bucket_name = get_bucket_name(environment)
  # If the bucket name is not found abort.
  if bucket_name:
    print("Using bucket {0}.".format(bucket_name))
    if check_package_exists(bucket_name, hex_checksum):
      print("Package is already uploaded.")
    else:
      print("Uploading new package.")
      upload_package(bucket_name, package_file, hex_checksum)

    # Parse the service.yml file and generate a Cloudformation template using Troposphere.
    print("Parsing {0}".format(service_yml_file))
    service_definition = parse_yaml(service_yml_file)
    service_name = service_definition['service']
    template = generate(service_definition)

    client = boto3.client('cloudformation')
    # Setup the stack name and parameters using values from service.yml and POWERUP_ENVIRONMENT.
    stack_name = '{0}-{1}'.format(environment.capitalize(), service_name.capitalize())
    print("Using stackname {0}".format(stack_name))

    stack_params = {
      'StackName': stack_name,
      'TemplateBody': template,
      'Capabilities': [ 'CAPABILITY_IAM' ],
      'Parameters': [
        {
          'ParameterKey': 'ServiceName',
          'ParameterValue': str(service_name)
        },
        {
          'ParameterKey': 'EnvironmentName',
          'ParameterValue': str(environment)
        },
        {
          'ParameterKey': 'LambdaPackageShaSum',
          'ParameterValue': str(base64_checksum)
        },
        {
          'ParameterKey': 'LambdaPackageS3Key',
          'ParameterValue': str(hex_checksum)
        }
      ]
    }

    # Check if the stack exists, then either create or update.
    stack_exists = True
    try:
      resp = client.describe_stacks(StackName=stack_name)
    except botocore.exceptions.ClientError:
      stack_exists = False

    if stack_exists:

      print("Stack found. Updating...")
      try:
        client.update_stack(**stack_params)
        waiter = client.get_waiter('stack_update_complete')
        waiter.config.delay = 1
        waiter.wait(StackName=stack_name)
        update_deployment()
      except botocore.exceptions.ClientError as e:
        print(e)
        print("No updates to be performed.")

    else:
      print("Stack not found. Creating...")
      client.create_stack(**stack_params)
      waiter = client.get_waiter('stack_create_complete')
      waiter.config.delay = 1
      waiter.wait(StackName=stack_name)
      update_deployment()

  else:
    print("Could not find bucket.")


else:
  print("No package found at {0}, aborting.".format(package_file))
