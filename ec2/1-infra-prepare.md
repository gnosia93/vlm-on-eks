
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
 
#### 1. kubectl 설치 #### 
```
ARCH=amd64     
curl -O https://s3.us-west-2.amazonaws.com/amazon-eks/1.33.3/2025-08-03/bin/linux/$ARCH/kubectl
chmod +x ./kubectl
mkdir -p $HOME/bin && cp ./kubectl $HOME/bin/kubectl && export PATH=$HOME/bin:$PATH
echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc

kubectl version --client
```

#### 2. eksctl 설치 ####
```
ARCH=amd64    
PLATFORM=$(uname -s)_$ARCH
curl -sLO "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_$PLATFORM.tar.gz"

tar -xzf eksctl_$PLATFORM.tar.gz -C /tmp && rm eksctl_$PLATFORM.tar.gz
sudo install -m 0755 /tmp/eksctl /usr/local/bin && rm /tmp/eksctl

eksctl version
```

#### 3. helm 설치 ####
```
curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4
sh get_helm.sh

helm version
``` 

#### 4. k9s 설치 ####
```
curl -sL https://github.com/derailed/k9s/releases/latest/download/k9s_Linux_amd64.tar.gz -o k9s.tar.gz
tar -xzf k9s.tar.gz k9s
sudo install k9s /usr/local/bin/
rm k9s.tar.gz k9s
```

#### 5. eks-node-viewer 설치 ####
```
sudo dnf update -y
sudo dnf install golang -y

# 설치 확인 (v1.11 이상 필요)
go version
go install github.com/awslabs/eks-node-viewer/cmd/eks-node-viewer@latest

echo 'export PATH=$PATH:$(go env GOPATH)/bin' >> ~/.bashrc
source ~/.bashrc
```
go 컴파일 과정에서 다소 시간이 소요된다.

### EKS 클러스터 생성하기 ###

```
export AWS_REGION=$(aws ec2 describe-availability-zones --query 'AvailabilityZones[0].RegionName' --output text)
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export CLUSTER_NAME="vlm-distillation"
export K8S_VERSION="1.34"
export KARPENTER_VERSION="1.8.1"
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=tag:Name,Values="${CLUSTER_NAME}" --query "Vpcs[].VpcId" --output text)

echo "AWS_REGION: $AWS_REGION"
echo "AWS_ACCOUNT_ID: $AWS_ACCOUNT_ID"
echo "CLUSTER_NAME: $CLUSTER_NAME"
echo "K8S_VERSION: $K8S_VERSION"
echo "KARPENTER_VERSION: $KARPENTER_VERSION"
echo "VPC_ID: $VPC_ID"
```

### 1. 서브넷 식별 ###
클러스터의 데이터 플레인(워커노드 들)은 아래의 프라이빗 서브넷에 위치하게 된다. 
```
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

# YAML 형식에 맞게 동적 문자열 생성 (각 ID 뒤에 ": {}" 추가 및 앞쪽 Identation과 줄바꿈)
SUBNET_YAML=""
if [ -f SUBNET_IDS ]; then
    rm SUBNET_IDS
fi
echo "$SUBNET_IDS" | while read -r az subnet_id;
do
    echo "      ${az}: { id: ${subnet_id} }" >> SUBNET_IDS
done
```

### 2. 클러스터 생성 ### 
클러스터 생성 완료까지 약 20 ~ 30분 정도의 시간이 소요된다.
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
```
```
eksctl create cluster -f cluster.yaml
```

[결과]
```
026-02-06 02:51:14 [ℹ]  eksctl version 0.221.0
2026-02-06 02:51:14 [ℹ]  using region ap-northeast-2
2026-02-06 02:51:14 [✔]  using existing VPC (vpc-07151864d34430640) and subnets (private:map[ap-northeast-2a:{subnet-09b68fab9521791fb ap-northeast-2a 10.0.10.0/24 0 } ap-northeast-2b:{subnet-08c3050a617b30d0e ap-northeast-2b 10.0.11.0/24 0 }] public:map[])
2026-02-06 02:51:14 [!]  custom VPC/subnets will be used; if resulting cluster doesn't function as expected, make sure to review the configuration of VPC/subnets
2026-02-06 02:51:14 [ℹ]  nodegroup "ng-arm" will use "" [AmazonLinux2023/1.34]
2026-02-06 02:51:14 [ℹ]  nodegroup "ng-x86" will use "" [AmazonLinux2023/1.34]
2026-02-06 02:51:14 [!]  Auto Mode will be enabled by default in an upcoming release of eksctl. This means managed node groups and managed networking add-ons will no longer be created by default. To maintain current behavior, explicitly set 'autoModeConfig.enabled: false' in your cluster configuration. Learn more: https://eksctl.io/usage/auto-mode/
2026-02-06 02:51:14 [ℹ]  using Kubernetes version 1.34
2026-02-06 02:51:14 [ℹ]  creating EKS cluster "get-started-eks" in "ap-northeast-2" region with managed nodes
2026-02-06 02:51:14 [ℹ]  2 nodegroups (ng-arm, ng-x86) were included (based on the include/exclude rules)
...
2026-02-06 03:11:52 [ℹ]  created namespace "karpenter"
2026-02-06 03:11:52 [ℹ]  created serviceaccount "karpenter/karpenter"
2026-02-06 03:11:52 [ℹ]  adding identity "arn:aws:iam::499514681453:role/eksctl-KarpenterNodeRole-get-started-eks" to auth ConfigMap
2026-02-06 03:11:52 [ℹ]  adding Karpenter to cluster get-started-eks
2026-02-06 03:12:12 [ℹ]  kubectl command should work with "/home/ec2-user/.kube/config", try 'kubectl get nodes'
2026-02-06 03:12:12 [✔]  EKS cluster "get-started-eks" in "ap-northeast-2" region is ready
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
----------------------------------------------------------------------------------------
|                                DescribeSecurityGroups                                |
+----------------------------------------+---------------------------------------------+
|                   Key                  |                    Value                    |
+----------------------------------------+---------------------------------------------+
|  aws:eks:cluster-name                  |  get-started-eks                            |
|  kubernetes.io/cluster/get-started-eks |  owned                                      |
|  Name                                  |  eks-cluster-sg-get-started-eks-1608279370  |
|  karpenter.sh/discovery                |  get-started-eks                            |
+----------------------------------------+---------------------------------------------+
```

### 3. 추가 정책 설정 ###
클러스터 생성이 완료되면 추가 설정이 필요하다. 카펜터 버전 1.8.1(EKS 1.3.4) 에는 아래와 같은 정책 설정이 누락되어 있어 패치가 필요하다. 
패치를 하지 않는 경우 카펜터가 프러비저닝한 노드가 클러스터에 조인되지 않는다. (노드 describe 시 Not Ready 상태)  

* eksctl-training-on-eks-iamservice-role 에 정책 추가(OIDC 정책 누락)
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



