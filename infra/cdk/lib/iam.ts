/** TASK-042: typed IAM JSON; no AWS calls or implicit account/model defaults. */
export interface PolicyStatement {
  Effect: "Allow" | "Deny";
  Action: string[];
  Resource: string[];
}

export interface ProofOpsIamProps {
  githubRepository: string;
  githubOidcProviderArn: string;
  artifactBucketArn: string;
  quarantineBucketArn: string;
  coreTableArn: string;
  auditTableArn: string;
  kmsKeyArn: string;
  secretsArns: string[];
  approvedModelArns: string[];
  approvedInferenceProfileArns: string[];
  deploymentStackArns: string[];
  ecrRepositoryArns: string[];
}

function exactArn(value: string, service: string): string {
  if (!value || /[*?\s]/.test(value) || !value.startsWith(`arn:aws:${service}:`)) {
    throw new Error(`Explicit ${service} ARN required`);
  }
  const fields = value.split(":");
  if (fields.length < 6 || !fields.slice(5).join(":")) throw new Error("Invalid ARN");
  if (service !== "s3" && (!/^[0-9]{12}$/.test(fields[4]) &&
      !(service === "bedrock" && fields[4] === "" && fields[5].startsWith("foundation-model/")))) {
    throw new Error("Explicit account required");
  }
  if (!["s3", "iam"].includes(service) && !fields[3]) throw new Error("Explicit region required");
  return value;
}

function models(props: ProofOpsIamProps): string[] {
  return [...props.approvedModelArns, ...props.approvedInferenceProfileArns].map(arn => {
    exactArn(arn, "bedrock");
    if (!/:(foundation-model|inference-profile|application-inference-profile)\/[^/]+$/.test(arn)) {
      throw new Error("Explicit Bedrock model/profile ARN required");
    }
    return arn;
  });
}

/** GitHub OIDC trusts protected environments only; branch/tag claims cannot bypass them. */
export function buildGithubDeployTrustPolicy(props: ProofOpsIamProps) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(props.githubRepository)) {
    throw new Error("Exact owner/repository required");
  }
  const provider = exactArn(props.githubOidcProviderArn, "iam");
  if (!provider.endsWith(":oidc-provider/token.actions.githubusercontent.com")) {
    throw new Error("GitHub OIDC provider required");
  }
  return {
    Version: "2012-10-17",
    Statement: [{
      Effect: "Allow",
      Principal: { Federated: provider },
      Action: "sts:AssumeRoleWithWebIdentity",
      Condition: { StringEquals: {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": [
          `repo:${props.githubRepository}:environment:staging`,
          `repo:${props.githubRepository}:environment:production`,
        ],
      } },
    }],
  };
}

/** Deployment service role/PassRole policy is a separate TASK-044 approved input. */
export function buildDeployRolePolicies(props: ProofOpsIamProps): PolicyStatement[] {
  if (!props.deploymentStackArns?.length || !props.ecrRepositoryArns?.length) {
    throw new Error("Explicit stack and repository ARNs required");
  }
  const statements: PolicyStatement[] = [
    { Effect: "Allow", Action: ["cloudformation:DescribeStacks", "cloudformation:CreateStack",
      "cloudformation:UpdateStack", "cloudformation:GetTemplate"],
      Resource: props.deploymentStackArns.map(arn => exactArn(arn, "cloudformation")) },
    // AWS ECR GetAuthorizationToken has no resource-level permission support.
    { Effect: "Allow", Action: ["ecr:GetAuthorizationToken"], Resource: ["*"] },
    { Effect: "Allow", Action: ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"],
      Resource: props.ecrRepositoryArns.map(arn => exactArn(arn, "ecr")) },
  ];
  assertNoWildcard(statements);
  return statements;
}

/** API has no model-invocation permission, even when a model is approved for the worker. */
export function buildApiTaskPolicies(props: ProofOpsIamProps): PolicyStatement[] {
  models(props); // Reject unsafe configuration even when API will not use it.
  const statements: PolicyStatement[] = [
    { Effect: "Allow", Action: ["s3:GetObject", "s3:PutObject"],
      Resource: [`${exactArn(props.artifactBucketArn, "s3")}/*`] },
    { Effect: "Allow", Action: ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
      Resource: [exactArn(props.coreTableArn, "dynamodb")] },
    { Effect: "Allow", Action: ["kms:Decrypt", "kms:GenerateDataKey"],
      Resource: [exactArn(props.kmsKeyArn, "kms")] },
  ];
  if (props.secretsArns.length) statements.push({ Effect: "Allow",
    Action: ["secretsmanager:GetSecretValue"],
    Resource: props.secretsArns.map(arn => exactArn(arn, "secretsmanager")) });
  assertNoWildcard(statements);
  return statements;
}

export function buildWorkerTaskPolicies(props: ProofOpsIamProps): PolicyStatement[] {
  const statements = buildApiTaskPolicies({ ...props, secretsArns: [] });
  const approved = models(props);
  if (approved.length) statements.push({ Effect: "Allow",
    Action: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource: approved });
  assertNoWildcard(statements);
  return statements;
}

/** Allow only bucket object suffixes and the documented ECR token exception. */
export function assertNoWildcard(statements: PolicyStatement[]): void {
  for (const statement of statements) {
    if (!statement.Action.length || !statement.Resource.length) throw new Error("Empty policy");
    for (const action of statement.Action) {
      if (/[*?]/.test(action)) throw new Error("Broad action forbidden");
    }
    for (const resource of statement.Resource) {
      if (resource === "*" && statement.Action.length === 1 &&
          statement.Action[0] === "ecr:GetAuthorizationToken") continue;
      if (/^arn:aws:s3:::[a-z0-9.-]+\/\*$/.test(resource) &&
          statement.Action.every(action => ["s3:GetObject", "s3:PutObject"].includes(action))) continue;
      if (!resource || /[*?]/.test(resource)) throw new Error("Broad resource forbidden");
    }
  }
}
