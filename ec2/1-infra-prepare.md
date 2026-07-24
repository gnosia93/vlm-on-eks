
## vpc 생성 ##

```
export AWS_REGION="ap-northeast-2"
export CF_STACK="vlm-distillation-`date +"%H-%M-%S"`"
echo "CF_STACK: $CF_STACK"

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
vpc 생성 진행 과정을 조회하고 완료될때 까지 대기한다. 
```
aws cloudformation wait stack-update-complete \
  --stack-name ${CF_STACK} \
  --region ${AWS_REGION}

aws cloudformation describe-stacks --stack-name ${CF_STACK} \
--region $AWS_REGION \
--query "Stacks[0].StackStatus"
```

생성 결과를 출력한다. 
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

## vpc 삭제하기 ##
```
aws cloudformation delete-stack --stack-name ${CF_STACK} --region $AWS_REGION
```
