### 버킷 삭제 ###
```
export ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
export REGION=ap-northeast-2
export AZ=ap-northeast-2b
export BUCKET=vlm-data-${ACCOUNT_ID}-${REGION}
```
```
aws s3 rm s3://$BUCKET --recursive   # 안의 객체 전부 삭제
aws s3api delete-bucket --bucket $BUCKET --region $REGION  # 그다음 버킷 삭제
```

### EKS 삭제 ###
* 카펜터 인스턴스 프로파일 삭제 
```
ROLE_NAME="eksctl-KarpenterNodeRole-${CLUSTER_NAME}"
for p in $(aws iam list-attached-role-policies --role-name "$ROLE_NAME" --query 'AttachedPolicies[*].PolicyArn' --output text); do aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$p"; done
for p in $(aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames[*]' --output text); do aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$p"; done
for i in $(aws iam list-instance-profiles-for-role --role-name "$ROLE_NAME" --query 'InstanceProfiles[*].InstanceProfileName' --output text); do aws iam remove-role-from-instance-profile --instance-profile-name "$i" --role-name "$ROLE_NAME"; aws iam delete-instance-profile --instance-profile-name "$i"; done
aws iam delete-role --role-name "$ROLE_NAME"
```
* 클러스터 삭제
```
eksctl delete cluster -f cluster.yaml
```

### vpc 삭제 ###
```
CF_STACK=$(cat CF_STACK | awk '{print $2}')
aws cloudformation delete-stack --stack-name ${CF_STACK} --region $AWS_REGION
```
