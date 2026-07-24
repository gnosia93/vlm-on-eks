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


### vpc 삭제 ###
```
CF_STACK=$(cat CF_STACK | awk '{print $2}')
aws cloudformation delete-stack --stack-name ${CF_STACK} --region $AWS_REGION
```
