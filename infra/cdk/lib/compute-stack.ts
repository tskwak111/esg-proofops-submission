/** SEC-002: CloudFormation resources only; synthesis makes no AWS calls.
 * TASK-044 must attach this security group to private task ENIs and supply a
 * reviewed image entrypoint, private endpoints and execution-role permissions.
 * No application task role is granted to the untrusted PDF inspection process.
 */
export interface QuarantineComputeProps {
  vpcId: string;
  endpointSecurityGroupIds: string[];
  s3PrefixListId: string;
  image: string;
  executionRoleArn: string;
}

export function buildQuarantineCompute(props: QuarantineComputeProps) {
  if (!/^vpc-[0-9a-f]+$/.test(props.vpcId) ||
      !/^pl-[0-9a-f]+$/.test(props.s3PrefixListId) ||
      !props.endpointSecurityGroupIds.length ||
      props.endpointSecurityGroupIds.some(id => !/^sg-[0-9a-f]+$/.test(id))) {
    throw new Error("Explicit private VPC and reviewed AWS endpoint IDs required");
  }
  if (!/^[A-Za-z0-9./_-]+@sha256:[0-9a-f]{64}$/.test(props.image) ||
      !/^arn:aws:iam::[0-9]{12}:role\/[A-Za-z0-9/_+=,.@-]+$/.test(props.executionRoleArn)) {
    throw new Error("Explicit image digest and execution role required");
  }
  return {
    AWSTemplateFormatVersion: "2010-09-09",
    Resources: {
      QuarantineSecurityGroup: {
        Type: "AWS::EC2::SecurityGroup",
        Properties: {
          GroupDescription: "Quarantine: HTTPS to reviewed AWS endpoints only",
          VpcId: props.vpcId,
          SecurityGroupIngress: [],
          // A nonempty explicit list avoids EC2's implicit allow-all default egress.
          SecurityGroupEgress: [
            ...props.endpointSecurityGroupIds.map(id => ({
              IpProtocol: "tcp", FromPort: 443, ToPort: 443,
              DestinationSecurityGroupId: id,
            })),
            { IpProtocol: "tcp", FromPort: 443, ToPort: 443,
              DestinationPrefixListId: props.s3PrefixListId },
          ],
        },
      },
      QuarantineTask: {
        Type: "AWS::ECS::TaskDefinition",
        Properties: {
          RequiresCompatibilities: ["FARGATE"], NetworkMode: "awsvpc",
          RuntimePlatform: { OperatingSystemFamily: "LINUX", CpuArchitecture: "X86_64" },
          Cpu: "2048", Memory: "8192", ExecutionRoleArn: props.executionRoleArn,
          ContainerDefinitions: [{
            Name: "quarantine", Image: props.image, Essential: true,
            User: "10001:10001", ReadonlyRootFilesystem: true,
            StopTimeout: 30,
            Environment: [{ Name: "PYTHONDONTWRITEBYTECODE", Value: "1" }],
            LinuxParameters: {
              InitProcessEnabled: true, Capabilities: { Drop: ["ALL"] },
              Tmpfs: [{ ContainerPath: "/tmp", Size: 256,
                MountOptions: ["rw", "noexec", "nosuid", "nodev", "mode=1777"] }],
            },
            // The verifier additionally sets Linux RLIMIT_NPROC=32 before parsing.
            Ulimits: [{ Name: "nofile", SoftLimit: 256, HardLimit: 256 }],
          }],
        },
      },
    },
    Outputs: {
      QuarantineSecurityGroupId: { Value: { Ref: "QuarantineSecurityGroup" } },
      QuarantineTaskDefinitionArn: { Value: { Ref: "QuarantineTask" } },
    },
  };
}
