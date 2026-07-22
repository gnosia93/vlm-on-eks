
```
export REGION=ap-northeast-2
export AZ=ap-northeast-2a

#### 1. VPC 생성하기 ####
```
VPC_ID=$(aws ec2 create-vpc \
  --region $REGION \
  --cidr-block 10.0.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=vlm-vpc}]' \
  --query 'Vpc.VpcId' --output text)

echo "VPC_ID=$VPC_ID"

# DNS 이름 해석 활성화 (퍼블릭 DNS 붙으려면 필요)
aws ec2 modify-vpc-attribute --region $REGION --vpc-id $VPC_ID --enable-dns-support
aws ec2 modify-vpc-attribute --region $REGION --vpc-id $VPC_ID --enable-dns-hostnames
```

#### 2. 인터넷 게이트웨이 생성 및 연결 #### 
```
IGW_ID=$(aws ec2 create-internet-gateway \
  --region $REGION \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=vlm-igw}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)

echo "IGW_ID=$IGW_ID"

aws ec2 attach-internet-gateway --region $REGION \
  --internet-gateway-id $IGW_ID --vpc-id $VPC_ID
```

#### 3. 퍼블릭 서브넷 생성 ####
```
SUBNET_ID=$(aws ec2 create-subnet \
  --region $REGION \
  --vpc-id $VPC_ID \
  --cidr-block 10.0.1.0/24 \
  --availability-zone $AZ \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=vlm-public-subnet}]' \
  --query 'Subnet.SubnetId' --output text)

echo "SUBNET_ID=$SUBNET_ID"

# 이 서브넷에서 뜨는 인스턴스에 퍼블릭 IP 자동 할당
aws ec2 modify-subnet-attribute --region $REGION \
  --subnet-id $SUBNET_ID --map-public-ip-on-launch
```

#### 4. 라우팅 테이블 (인터넷 경로 추가) ####
```
RTB_ID=$(aws ec2 create-route-table \
  --region $REGION \
  --vpc-id $VPC_ID \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=vlm-public-rt}]' \
  --query 'RouteTable.RouteTableId' --output text)

echo "RTB_ID=$RTB_ID"

# 0.0.0.0/0 → 인터넷 게이트웨이
aws ec2 create-route --region $REGION \
  --route-table-id $RTB_ID \
  --destination-cidr-block 0.0.0.0/0 \
  --gateway-id $IGW_ID

# 서브넷에 라우팅 테이블 연결
aws ec2 associate-route-table --region $REGION \
  --route-table-id $RTB_ID --subnet-id $SUBNET_ID
```

#### 5. 보안 그룹 (SSH 허용) ####
```
SG_ID=$(aws ec2 create-security-group \
  --region $REGION \
  --group-name vlm-sg \
  --description "SSH for vlm infer" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)

echo "SG_ID=$SG_ID"

# 내 IP에서만 SSH 허용 (권장)
MY_IP=$(curl -s https://checkip.amazonaws.com)

aws ec2 authorize-security-group-ingress --region $REGION \
  --group-id $SG_ID \
  --protocol tcp --port 22 --cidr ${MY_IP}/32
```



