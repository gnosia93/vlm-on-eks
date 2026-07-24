
## vpc 생성 ##

```
export AWS_REGION="ap-northeast-2"
export CF_STACK="vlm-distillation-`date +"%H-%M-%S"`"
echo "CF_STACK: $CF_STACK" | tee CF_STACK	

cd ~
git clone https://github.com/gnosia93/vlm-distillation.git
cd ~/vlm-distillation
pwd


MY_IP="$(curl -s https://checkip.amazonaws.com)""/32"
sed -i "" "s|\${MY_IP}|$MY_IP|g" $(pwd)/src/cf/eks-vpc.yaml
echo ${MY_IP}

aws cloudformation create-stack \
  --region ${AWS_REGION} \
  --stack-name ${CF_STACK} \
  --template-body file://$(pwd)/src/cf/eks-vpc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags Key=Project,Value=vlm-distillation
```
vpc 생성이 완료될때 까지 대기한다. 
```
aws cloudformation describe-stacks --stack-name ${CF_STACK} \
--region $AWS_REGION \
--query "Stacks[0].StackStatus"
```
[결과]
```
"CREATE_COMPLETE"
```

vs-code 웹 URL 을 출력한다. 
```
OUTPUT=$(aws cloudformation describe-stacks --region ${AWS_REGION} \
  --stack-name ${CF_STACK} \
  --query "Stacks[0].Outputs[?OutputKey=='X86VsCode'].\
  {Name: OutputKey, Value: OutputValue}" \
  --output json)
echo ${OUTPUT}
```
[결과]
```
[
    {
        "Name": "X86VsCode",
        "Value": "http://ec2-43-201-103-189.ap-northeast-2.compute.amazonaws.com:8080"
    }
]
```

## EKS 프로비저닝 ##

vs-code 서버에 웹으로 접속한 후, 터미널을 열어 kubectl, eksctl, helm 을 설치한다. (vs-code 패스워드는 code!@#c 이다)
![](https://github.com/gnosia93/training-on-eks/blob/main/chapter/images/code-server.png)
 
### 1. 소프트웨어 설치 ### 
```
ARCH=amd64     
PLATFORM=$(uname -s)_$ARCH

curl -O https://s3.us-west-2.amazonaws.com/amazon-eks/1.33.3/2025-08-03/bin/linux/$ARCH/kubectl
chmod +x ./kubectl
mkdir -p $HOME/bin && cp ./kubectl $HOME/bin/kubectl && export PATH=$HOME/bin:$PATH
echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc

curl -sLO "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_$PLATFORM.tar.gz"
tar -xzf eksctl_$PLATFORM.tar.gz -C /tmp && rm eksctl_$PLATFORM.tar.gz
sudo install -m 0755 /tmp/eksctl /usr/local/bin && rm /tmp/eksctl

curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4
sh get_helm.sh

curl -sL https://github.com/derailed/k9s/releases/latest/download/k9s_Linux_amd64.tar.gz -o k9s.tar.gz
tar -xzf k9s.tar.gz k9s
sudo install k9s /usr/local/bin/
rm k9s.tar.gz k9s

sudo dnf update -y
sudo dnf install golang -y
go install github.com/awslabs/eks-node-viewer/cmd/eks-node-viewer@latest
echo 'export PATH=$PATH:$(go env GOPATH)/bin' >> ~/.bashrc
source ~/.bashrc


kubectl version --client
eksctl version
helm version
``` 

### 2. eksctl로 클러스터 생성 ###

워커노드 들은 식별된 프라이빗 서브넷에 위치하게 된다. 
```
export AWS_REGION=$(aws ec2 describe-availability-zones --query 'AvailabilityZones[0].RegionName' --output text)
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export CLUSTER_NAME="vlm-distillation"
export K8S_VERSION="1.34"
export KARPENTER_VERSION="1.8.1"
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=tag:Name,Values="${CLUSTER_NAME}" --query "Vpcs[].VpcId" --output text)

echo -e "\n------------------------------------------"
echo "AWS_REGION: $AWS_REGION"
echo "AWS_ACCOUNT_ID: $AWS_ACCOUNT_ID"
echo "CLUSTER_NAME: $CLUSTER_NAME"
echo "K8S_VERSION: $K8S_VERSION"
echo "KARPENTER_VERSION: $KARPENTER_VERSION"
echo "VPC_ID: $VPC_ID"


aws ec2 describe-subnets \
    --filters "Name=tag:Name,Values=vlm-priv-subnet-*" "Name=vpc-id,Values=${VPC_ID}" \
    --query "Subnets[*].{ID:SubnetId, AZ:AvailabilityZone, Name:Tags[?Key=='Name']|[0].Value}" \
    --output table

SUBNET_IDS=$(aws ec2 describe-subnets \
    --filters "Name=tag:Name,Values=vlm-priv-subnet-*" "Name=vpc-id,Values=${VPC_ID}" \
    --query "Subnets[*].{ID:SubnetId, AZ:AvailabilityZone}" \
    --output text)

if [ -z "$SUBNET_IDS" ]; then
    echo "에러: VPC ${VPC_ID} 에 서브넷이 존재하지 않습니다.."
fi

SUBNET_YAML=""
if [ -f SUBNET_IDS ]; then
    rm SUBNET_IDS
fi
echo "$SUBNET_IDS" | while read -r az subnet_id;
do
    echo "      ${az}: { id: ${subnet_id} }" >> SUBNET_IDS
done
```

클러스터 생성 완료까지 약 20 분 정도의 시간이 소요된다.
```
cat > cluster.yaml <<EOF 
---
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: "${CLUSTER_NAME}"
  version: "${K8S_VERSION}"
  region: "${AWS_REGION}"

vpc:
  id: "${VPC_ID}"                    
  subnets:
    private:                                 # 프라이빗 서브넷에 데이터플레인 설치
$(cat SUBNET_IDS)

addons:
  - name: vpc-cni
    podIdentityAssociations:
      - serviceAccountName: aws-node
        namespace: kube-system
        permissionPolicyARNs: 
          - arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy
  - name: eks-pod-identity-agent
  - name: metrics-server
  - name: kube-proxy
  - name: coredns
  - name: aws-ebs-csi-driver                   

managedNodeGroups:                           # 관리형 노드 그룹
  - name: ng-x86
    instanceType: c6i.2xlarge
    minSize: 2
    maxSize: 2
    desiredCapacity: 2
    amiFamily: AmazonLinux2023
    privateNetworking: true           		 # 이 노드 그룹이 PRIVATE 서브넷만 사용하도록 지정합니다. 
    iam:
      withAddonPolicies:
        ebs: true                     		 # EBS CSI 드라이버가 작동하기 위한 IAM 권한 부여

iam:
  withOIDC: true 

karpenter:
  version: "${KARPENTER_VERSION}"
EOF

eksctl create cluster -f cluster.yaml
```

[결과]
```
2026-07-24 16:13:45 [ℹ]  eksctl version 0.229.0
2026-07-24 16:13:45 [ℹ]  using region ap-northeast-2
2026-07-24 16:13:45 [✔]  using existing VPC (vpc-01531b61ba2272a88) and subnets (private:map[ap-northeast-2a:{subnet-0b7d8aceb1614c6fd ap-northeast-2a 10.0.10.0/24 0 } ap-northeast-2b:{subnet-0bfab250da388270c ap-northeast-2b 10.0.11.0/24 0 }] public:map[])
2026-07-24 16:13:45 [!]  custom VPC/subnets will be used; if resulting cluster doesn't function as expected, make sure to review the configuration of VPC/subnets
2026-07-24 16:13:45 [ℹ]  nodegroup "ng-x86" will use "" [AmazonLinux2023/1.34]
2026-07-24 16:13:45 [!]  Auto Mode will be enabled by default in an upcoming release of eksctl. This means managed node groups and managed networking add-ons will no longer be created by default. To maintain current behavior, explicitly set 'autoModeConfig.enabled: false' in your cluster configuration. Learn more: https://eksctl.io/usage/auto-mode/
2026-07-24 16:13:45 [ℹ]  using Kubernetes version 1.34
2026-07-24 16:13:45 [ℹ]  creating EKS cluster "vlm-distillation" in "ap-northeast-2" region with managed nodes
2026-07-24 16:13:45 [ℹ]  1 nodegroup (ng-x86) was included (based on the include/exclude rules)
2026-07-24 16:13:45 [ℹ]  will create a CloudFormation stack for cluster itself and 1 managed nodegroup stack(s)
2026-07-24 16:13:45 [ℹ]  if you encounter any issues, check CloudFormation console or try 'eksctl utils describe-stacks --region=ap-northeast-2 --cluster=vlm-distillation'
2026-07-24 16:13:45 [ℹ]  Kubernetes API endpoint access will use default of {publicAccess=true, privateAccess=false} for cluster "vlm-distillation" in "ap-northeast-2"
2026-07-24 16:13:45 [ℹ]  CloudWatch logging will not be enabled for cluster "vlm-distillation" in "ap-northeast-2"
2026-07-24 16:13:45 [ℹ]  you can enable it with 'eksctl utils update-cluster-logging --enable-types={SPECIFY-YOUR-LOG-TYPES-HERE (e.g. all)} --region=ap-northeast-2 --cluster=vlm-distillation'
2026-07-24 16:13:45 [ℹ]  
2 sequential tasks: { create cluster control plane "vlm-distillation", 
    2 sequential sub-tasks: { 
        5 sequential sub-tasks: { 
            1 task: { create addons },
            wait for control plane to become ready,
            associate IAM OIDC provider,
            no tasks,
            update VPC CNI to use IRSA if required,
        },
        create managed nodegroup "ng-x86",
    } 
}
2026-07-24 16:13:45 [ℹ]  building cluster stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:13:45 [ℹ]  deploying stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:14:15 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:14:45 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:15:45 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:16:45 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:17:46 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:18:46 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:19:46 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:20:46 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:21:46 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-cluster"
2026-07-24 16:21:47 [ℹ]  creating addon: eks-pod-identity-agent
2026-07-24 16:21:48 [ℹ]  successfully created addon: eks-pod-identity-agent
2026-07-24 16:21:48 [ℹ]  pod identity associations are set for "vpc-cni" addon; will use these to configure required IAM permissions
2026-07-24 16:21:48 [ℹ]  deploying stack "eksctl-vlm-distillation-addon-vpc-cni-podidentityrole-aws-node"
2026-07-24 16:21:48 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-addon-vpc-cni-podidentityrole-aws-node"
2026-07-24 16:22:18 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-addon-vpc-cni-podidentityrole-aws-node"
2026-07-24 16:22:18 [ℹ]  creating addon: vpc-cni
2026-07-24 16:22:20 [ℹ]  successfully created addon: vpc-cni
2026-07-24 16:22:20 [ℹ]  creating addon: kube-proxy
2026-07-24 16:22:21 [ℹ]  successfully created addon: kube-proxy
2026-07-24 16:22:21 [ℹ]  creating addon: coredns
2026-07-24 16:22:21 [ℹ]  successfully created addon: coredns
2026-07-24 16:24:23 [ℹ]  addon "vpc-cni" active
2026-07-24 16:24:24 [ℹ]  updating IAM resources stack "eksctl-vlm-distillation-addon-vpc-cni-podidentityrole-aws-node" for pod identity association "a-amlxhmzglci1cyqeb"
2026-07-24 16:24:24 [ℹ]  waiting for CloudFormation changeset "eksctl-kube-system-aws-node-update-1784910264" for stack "eksctl-vlm-distillation-addon-vpc-cni-podidentityrole-aws-node"
2026-07-24 16:24:24 [ℹ]  nothing to update
2026-07-24 16:24:24 [ℹ]  IAM resources for kube-system/aws-node (pod identity association ID: a-amlxhmzglci1cyqeb) are already up-to-date
2026-07-24 16:24:24 [ℹ]  updating addon
2026-07-24 16:24:35 [ℹ]  addon "vpc-cni" active
2026-07-24 16:24:35 [ℹ]  building managed nodegroup stack "eksctl-vlm-distillation-nodegroup-ng-x86"
2026-07-24 16:24:35 [ℹ]  deploying stack "eksctl-vlm-distillation-nodegroup-ng-x86"
2026-07-24 16:24:36 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-nodegroup-ng-x86"
2026-07-24 16:25:06 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-nodegroup-ng-x86"
2026-07-24 16:25:52 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-nodegroup-ng-x86"
2026-07-24 16:27:41 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-nodegroup-ng-x86"
2026-07-24 16:27:41 [ℹ]  waiting for the control plane to become ready
2026-07-24 16:27:41 [✔]  saved kubeconfig as "/home/ec2-user/.kube/config"
2026-07-24 16:27:41 [ℹ]  no tasks
2026-07-24 16:27:41 [✔]  all EKS cluster resources for "vlm-distillation" have been created
2026-07-24 16:27:41 [ℹ]  nodegroup "ng-x86" has 2 node(s)
2026-07-24 16:27:41 [ℹ]  node "ip-10-0-10-207.ap-northeast-2.compute.internal" is ready
2026-07-24 16:27:41 [ℹ]  node "ip-10-0-11-229.ap-northeast-2.compute.internal" is ready
2026-07-24 16:27:41 [ℹ]  waiting for at least 2 node(s) to become ready in "ng-x86"
2026-07-24 16:27:41 [ℹ]  nodegroup "ng-x86" has 2 node(s)
2026-07-24 16:27:41 [ℹ]  node "ip-10-0-10-207.ap-northeast-2.compute.internal" is ready
2026-07-24 16:27:41 [ℹ]  node "ip-10-0-11-229.ap-northeast-2.compute.internal" is ready
2026-07-24 16:27:41 [✔]  created 1 managed nodegroup(s) in cluster "vlm-distillation"
2026-07-24 16:27:42 [ℹ]  creating addon: metrics-server
2026-07-24 16:28:17 [ℹ]  addon "metrics-server" active
2026-07-24 16:28:18 [!]  the recommended way to provide IAM permissions for "aws-ebs-csi-driver" addon is via pod identity associations; after addon creation is completed, run `eksctl utils migrate-to-pod-identity`
2026-07-24 16:28:18 [ℹ]  creating role using recommended policies for "aws-ebs-csi-driver" addon
2026-07-24 16:28:18 [ℹ]  deploying stack "eksctl-vlm-distillation-addon-aws-ebs-csi-driver"
2026-07-24 16:28:18 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-addon-aws-ebs-csi-driver"
2026-07-24 16:28:48 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-addon-aws-ebs-csi-driver"
2026-07-24 16:28:48 [ℹ]  creating addon: aws-ebs-csi-driver
2026-07-24 16:29:17 [ℹ]  addon "aws-ebs-csi-driver" active
2026-07-24 16:29:18 [ℹ]  1 task: { create karpenter for stack "vlm-distillation" }
2026-07-24 16:29:18 [ℹ]  building nodegroup stack "eksctl-vlm-distillation-karpenter"
2026-07-24 16:29:18 [ℹ]  deploying stack "eksctl-vlm-distillation-karpenter"
2026-07-24 16:29:18 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-karpenter"
2026-07-24 16:29:48 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-karpenter"
2026-07-24 16:30:45 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-karpenter"
2026-07-24 16:31:43 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-karpenter"
2026-07-24 16:33:41 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-karpenter"
2026-07-24 16:33:41 [ℹ]  karpenter.createServiceAccount=false: eksctl will create both the IAM role and the "karpenter" service account in namespace "karpenter"
2026-07-24 16:33:41 [ℹ]  1 task: { 
    2 sequential sub-tasks: { 
        create IAM role for serviceaccount "karpenter/karpenter",
        create serviceaccount "karpenter/karpenter",
    } }2026-07-24 16:33:41 [ℹ]  1 task: { 
    2 sequential sub-tasks: { 
        create IAM role for serviceaccount "karpenter/karpenter",
        create serviceaccount "karpenter/karpenter",
    } }2026-07-24 16:33:41 [ℹ]  building iamserviceaccount stack "eksctl-vlm-distillation-addon-iamserviceaccount-karpenter-karpenter"
2026-07-24 16:33:41 [ℹ]  deploying stack "eksctl-vlm-distillation-addon-iamserviceaccount-karpenter-karpenter"
2026-07-24 16:33:41 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-addon-iamserviceaccount-karpenter-karpenter"
2026-07-24 16:34:11 [ℹ]  waiting for CloudFormation stack "eksctl-vlm-distillation-addon-iamserviceaccount-karpenter-karpenter"
2026-07-24 16:34:12 [ℹ]  created namespace "karpenter"
2026-07-24 16:34:12 [ℹ]  created serviceaccount "karpenter/karpenter"
2026-07-24 16:34:12 [ℹ]  adding identity "arn:aws:iam::499514681453:role/eksctl-KarpenterNodeRole-vlm-distillation" to auth ConfigMap
2026-07-24 16:34:12 [ℹ]  adding Karpenter to cluster vlm-distillation
2026-07-24 16:34:30 [ℹ]  kubectl command should work with "/home/ec2-user/.kube/config", try 'kubectl get nodes'
2026-07-24 16:34:30 [✔]  EKS cluster "vlm-distillation" in "ap-northeast-2" region is ready
```

EKS 에서 클러스터 시큐리티 그룹은 컨트롤 플레인과 워커노드 사이의 통신을 가능하게 한다. 컨트롤 플레인은 10250 포트를 통해 노드의 큐블렛과 통신하고 워커노드는 443 포트를 이용하여 컨트롤 플레인의 API 서버에 접근을 시도한다. 아래 명령어는 클러스터 시큐리티 그룹에 "karpenter.sh/discovery=${CLUSTER_NAME}" 태크가 존재하는지 확인하는 스크립트이다. 카펜터가 노드를 생성할때, 이와 동일한 태크를 가진 시큐리티 그룹을 찾아 신규 노드에 할당하게 된다. 시큐리티 그룹 검색에 실패하게 되는 경우, EC2 인스턴스는 생성되지만 EKS 클러스터에 조인하지 못한다.  
```
aws ec2 create-tags \
  --resources $(aws eks describe-cluster --name ${CLUSTER_NAME} --query \
					"cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text) \
  --tags Key=karpenter.sh/discovery,Value=${CLUSTER_NAME}
```
또한 쿠버네티스의 서비스 타입을 Load Balancer 변경시 CLB(Classsic Load Balancer)가 생성되는데, 태그가 없는 경우 CLB는 생성되나 해당 서비스의 Pod와 통신이 되지 않는다.  
```
aws ec2 describe-security-groups \
  --group-ids $(aws eks describe-cluster --name ${CLUSTER_NAME} --query \
					"cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text) \
  --query "SecurityGroups[0].Tags" \
  --output table
```
[결과]
```
------------------------------------------------------------------------------------------
|                                 DescribeSecurityGroups                                 |
+-----------------------------------------+----------------------------------------------+
|                   Key                   |                    Value                     |
+-----------------------------------------+----------------------------------------------+
|  kubernetes.io/cluster/vlm-distillation |  owned                                       |
|  karpenter.sh/discovery                 |  vlm-distillation                            |
|  Name                                   |  eks-cluster-sg-vlm-distillation-1546968536  |
|  aws:eks:cluster-name                   |  vlm-distillation                            |
+-----------------------------------------+----------------------------------------------+
```

#### 3. 추가 정책 설정 ####
클러스터 생성이 완료되면 추가 설정이 필요하다. 카펜터 버전 1.8.1(EKS 1.3.4) 에는 아래와 같은 정책 설정이 누락되어 있어 패치가 필요하다. 
패치를 하지 않는 경우 카펜터가 프러비저닝한 노드가 클러스터에 조인되지 않는다. (노드 describe 시 Not Ready 상태)  

* eksctl-vlm-distillation-iamservice-role 에 정책 추가(OIDC 정책 누락)
```
POLICY_JSON=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "VisualEditor0",
            "Effect": "Allow",
            "Action": "eks:DescribeCluster",
            "Resource": "arn:aws:eks:${AWS_REGION}:${AWS_ACCOUNT_ID}:cluster/${CLUSTER_NAME}"
        },
        {
            "Effect": "Allow",
            "Action": [
                "iam:CreateInstanceProfile",
                "iam:DeleteInstanceProfile",
                "iam:GetInstanceProfile",
                "iam:TagInstanceProfile",
                "iam:AddRoleToInstanceProfile",
                "iam:RemoveRoleFromInstanceProfile",
                "iam:ListInstanceProfiles"
            ],
            "Resource": "*"
        }
    ]
}
EOF
)

aws iam put-role-policy \
    --role-name eksctl-${CLUSTER_NAME}-iamservice-role \
    --policy-name EKS_OIDC_Support_Policy \
    --policy-document "$POLICY_JSON"
```

## GPU 스케줄링 ##

### 1. 디바이스 플러그인 설치 ###
```
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm repo update
helm search repo nvdp --devel

helm install nvdp nvdp/nvidia-device-plugin \
  --namespace nvidia \
  --create-namespace \
  --version 0.18.2 \
  --set gfd.enabled=true

kubectl get daemonset -n nvidia
```

### 2. GPU 노드풀 생성 ###
```
cat <<EOF > nodepool-gpu.yaml 
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu
spec:
  template:
    metadata:
      labels:
        nodeType: "nvidia" 
    spec:
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand", "reserved"]
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["g", "p"]
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu
      expireAfter: 720h # 30 * 24h = 720h
      taints:
      - key: "nvidia.com/gpu"            # nvidia-device-plugin 데몬은 nvidia.com/gpu=present:NoSchedule 테인트를 Tolerate 한다. 
        value: "present"                 # value 값으로 present 와 다른값을 설정하면 nvidia-device-plugin 이 동작하지 않는다 (GPU를 찾을 수 없다)   
        effect: NoSchedule               # nvidia-device-plugin 이 GPU 를 찾으면 Nvidia GPU 관련 각종 테인트와 레이블 등을 노드에 할당한다.  
  limits:
    cpu: 1000
  disruption:
    consolidationPolicy: WhenEmpty       # 이전 설정값은 WhenEmptyOrUnderutilized / 노드의 잦은 Not Ready 상태로의 변경으로 인해 수정  
    consolidateAfter: 20m
---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu
spec:
  role: "eksctl-KarpenterNodeRole-${CLUSTER_NAME}"
  amiSelectorTerms:
    # Required; when coupled with a pod that requests NVIDIA GPUs or AWS Neuron
    # devices, Karpenter will select the correct AL2023 accelerated AMI variant
    # see https://aws.amazon.com/ko/blogs/containers/amazon-eks-optimized-amazon-linux-2023-accelerated-amis-now-available/
    # EKS GPU Optimized AMI: NVIDIA 드라이버와 CUDA 런타임만 포함된 가벼운 이미지 (Karpenter가 자동으로 선택 가능) 가 설치됨.
    # 특정 DLAMI 가 필요한 경우 - name : 필드에 정의해야 함. 
    - alias: al2023@latest
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "${CLUSTER_NAME}" 
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "${CLUSTER_NAME}" 
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 600Gi
        volumeType: gp3
EOF

kubectl apply -f nodepool-gpu.yaml
```
Ready 상태값이 True 임을 확인한다. 
```
kubectl get ec2nodeclass,nodepool
```
[결과]
```
NAME                                 READY   AGE
ec2nodeclass.karpenter.k8s.aws/gpu   True    17s

NAME                        NODECLASS   NODES   READY   AGE
nodepool.karpenter.sh/gpu   gpu         0       True    17s
```

### 3. nvidia-smi 파드 스케줄링 ###
```
cat <<EOF | kubectl apply -f - 
apiVersion: v1
kind: Pod
metadata:
  name: gpu-pod
spec:
  restartPolicy: Never                                # 재시작 정책을 Never로 설정 (실행 완료 후 다시 시작하지 않음)
  containers:                                         # 기본값은 Always - 컨테이너가 성공적으로 종료(exit 0)되든, 에러로 종료(exit nonzero)되든 상관없이 항상 재시작
    - name: cuda-container                            # nvidia-smi만 실행하고 끝나는 파드에 이 정책이 적용되면, 종료 후 다시 실행을 반복하다가 결국 CrashLoopBackOff 상태가 됨.
      image: nvidia/cuda:13.0.2-runtime-ubuntu22.04    
      command: ["/bin/sh", "-c"]
      args: ["nvidia-smi && sleep 300"]                # nvidia-smi 실행 후 300초(5분) 동안 대기
      resources:
        limits:
          nvidia.com/gpu: 1
  tolerations:                                             
    - key: "nvidia.com/gpu"
      operator: "Exists"                      # 노드의 테인트는 nvidia.com/gpu=present:NoSchedule 이나,   
      effect: "NoSchedule"                    # Exists 연산자로 nvidia.com/gpu 키만 체크         
EOF
```

파드를 생성하고 nvidia-smi 가 동작하는 확인한다.  
```
kubectl get pods
kubectl logs gpu-pod
```
[출력]
```
Wed Dec 10 06:44:46 2025       
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 570.195.03             Driver Version: 570.195.03     CUDA Version: 13.0     |
|-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|                                         |                        |               MIG M. |
|=========================================+========================+======================|
|   0  NVIDIA T4G                     On  |   00000000:00:1F.0 Off |                    0 |
| N/A   49C    P8              9W /   70W |       0MiB /  15360MiB |      0%      Default |
|                                         |                        |                  N/A |
+-----------------------------------------+------------------------+----------------------+
                                                                                         
+-----------------------------------------------------------------------------------------+
| Processes:                                                                              |
|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |
|        ID   ID                                                               Usage      |
|=========================================================================================|
|  No running processes found                                                             |
+-----------------------------------------------------------------------------------------+
```

## EFA ##

### 1. EFA 플러그인 설치 ###
```
helm repo add eks https://aws.github.io/eks-charts
helm install aws-efa-k8s-device-plugin eks/aws-efa-k8s-device-plugin --namespace kube-system

kubectl patch ds aws-efa-k8s-device-plugin -n kube-system --type='json' -p='[
  {"op": "add", "path": "/spec/template/spec/tolerations/-", "value": {"operator": "Exists"}}
]'

kubectl get ds aws-efa-k8s-device-plugin -n kube-system
```

### 2. EFA 테스트 ###
```
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: efa-test-pod
  labels:
    app: efa-test
spec:
  nodeSelector:
    karpenter.sh/nodepool: gpu                
  tolerations:                                             
    - key: "nvidia.com/gpu"
      operator: "Exists"                      # 노드의 테인트는 nvidia.com/gpu=present:NoSchedule 이나, Exists 연산자로 nvidia.com/gpu 키만 체크  
      effect: "NoSchedule"
#    - key: "vpc.amazon.com/efa"              # 카펜터 gpu 풀의 노드들은 nvidia.com/gpu 테인트만 가지고 있다.
#      operator: "Exists"                       
#      effect: "NoSchedule"        
  containers:
    - name: efa-container                               # public.ecr.aws/deep-learning-containers/pytorch-training:2.8.0-gpu-py312-cu129-ubuntu22.04-ec2-v1.0 
      image: public.ecr.aws/hpc-cloud/nccl-tests:latest           # EFA 드라이버와 NCCL 테스트 도구가 포함된 이미지 사용 (NVIDIA 공식 이미지 권장)
      command: ["/bin/bash", "-c", "sleep infinity"]
      resources:
        limits:
          vpc.amazonaws.com/efa: 1                      # EFA 장치를 파드에 직접 할당 (VPC CNI가 이 장치를 인식함)
          nvidia.com/gpu: 1                             # GPU 인스턴스인 경우
      securityContext:
        capabilities:                                   # EFA 통신을 위해 메모리 잠금 권한 필요
          add: ["IPC_LOCK"]
EOF
```
아래 명령어로 efa-test-pod 가 EFA 지원노드에 스케줄링 된 것을 확인 한 후 (Running), 
```
kubectl get pods
```
[결과]
```
NAME           READY   STATUS      RESTARTS   AGE
efa-test-pod   1/1     Running     0          3m20s
gpu-pod        0/1     Completed   0          17m
```

efa-test-pod 파드에 로그인하여 efa 디바이스 정보를 조회한다. 
```
kubectl exec -it efa-test-pod -- /bin/bash
fi_info -p efa
ls -la /sys/class/infiniband/
```


## 큐브플로우 Trainer 설치 ##
```
sudo dnf install git -y
export VERSION=v2.1.0
kubectl apply --server-side -k "https://github.com/kubeflow/trainer.git/manifests/overlays/manager?ref=${VERSION}"
```
10 초 정도 지나후에 클러스터 트레이닝런타임을 설치한다. 
```
kubectl apply --server-side -k "https://github.com/kubeflow/trainer.git/manifests/overlays/runtimes?ref=${VERSION}"
kubectl get clustertrainingruntimes
```
[결과]
```
NAME                     AGE
deepspeed-distributed    9s
mlx-distributed          9s
torch-distributed        9s
torchtune-llama3.2-1b    9s
torchtune-llama3.2-3b    9s
torchtune-qwen2.5-1.5b   9s
```

* efa 관련 설정을 추가하기 위해 torch-distributed 런타임을 수정한다. 
```
$ kubectl edit clustertrainingruntime torch-distributed 

apiVersion: trainer.kubeflow.org/v1alpha1
kind: ClusterTrainingRuntime
metadata:
  name: torch-distributed
spec:
  template:
    spec:
      shareProcessNamespace: true           # 추가 
      hostIPC: true                         # 추가
      containers:
        - name: node
          # EFA 및 분산 학습을 위한 보안 설정 추가
          securityContext:
            privileged: true                 # 호스트 시스템의 모든 리소스(디바이스)와 커널 기능에 대한 완전한 접근 권한을 부여하는 설정
            capabilities:
              add: ["IPC_LOCK"]
```
아래 명령어로 제대로 수정되었는지 확인한다.
```
kubectl get clustertrainingruntime torch-distributed -o yaml
```

> [!NOTE]
> trainjob 명령어
> * 잡 확인 - kubectl get trainjob                       
> * 잡 삭제 - kubectl delete trainjob llama-3-8b        
> * 잡 상세 - kubectl describe trainjob llama-3-8b
    





