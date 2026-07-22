## VPC 생성하기 ##

### 1. 환경설정 ###
```
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export REGION=ap-northeast-2
export AZ=ap-northeast-2b
export BUCKET=vlm-data-${ACCOUNT_ID}-${REGION}
```

### 2. VPC 생성 ###
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

### 3. 인터넷 게이트웨이 생성 ### 
```
IGW_ID=$(aws ec2 create-internet-gateway \
  --region $REGION \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=vlm-igw}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)

echo "IGW_ID=$IGW_ID"

aws ec2 attach-internet-gateway --region $REGION \
  --internet-gateway-id $IGW_ID --vpc-id $VPC_ID
```

### 4. 퍼블릭 서브넷 생성 ###
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

### 5. 라우팅 테이블 (인터넷 경로 추가) ###
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

### 6. 보안 그룹 (SSH 허용) ###
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
echo "MY_IP=$MY_IP"

aws ec2 authorize-security-group-ingress --region $REGION \
  --group-id $SG_ID \
  --protocol tcp --port 22 --cidr ${MY_IP}/32
```

### 7. S3 버킷 생성 ###
```
echo "BUCKET=$BUCKET"

aws s3api create-bucket \
  --bucket $BUCKET \
  --region $REGION \
  --create-bucket-configuration LocationConstraint=$REGION
```

### 8. 인스턴스 프로파일 생성 ###
```
cat > trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "ec2.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name vlm-ec2-role \
  --assume-role-policy-document file://trust-policy.json

cat > s3-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::${BUCKET}"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name vlm-ec2-role \
  --policy-name vlm-s3-access \
  --policy-document file://s3-policy.json

aws iam create-instance-profile \
  --instance-profile-name vlm-ec2-profile

aws iam add-role-to-instance-profile \
  --instance-profile-name vlm-ec2-profile \
  --role-name vlm-ec2-role
```
ssh 대신 system manager 로 접속하기 위해서 AmazonSSMManagedInstanceCore 정책을 추가한다.  
```
aws iam attach-role-policy \
  --role-name vlm-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

### 9. 결과정리 ###
```
echo "VPC_ID=$VPC_ID"
echo "SUBNET_ID=$SUBNET_ID   (--subnet-id 에 사용)"
echo "SG_ID=$SG_ID           (--security-group-ids 에 사용)"
echo "BUCKET=$BUCKET"
echo "instance profile name -> vlm-ec2-profile"
```

[결과]
```
VPC_ID=vpc-0d5138774d93063ad
SUBNET_ID=subnet-01c84e439c1bd6dbc   (--subnet-id 에 사용)
SG_ID=sg-093c1f88dbf1f229b           (--security-group-ids 에 사용)
```


