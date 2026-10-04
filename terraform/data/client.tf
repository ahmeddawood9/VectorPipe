# Temporary test client in a PRIVATE subnet, reached via SSM Session Manager:
# no inbound ports, no SSH keys, access is authenticated by IAM.

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_iam_policy_document" "client_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "client" {
  name               = "${var.project}-db-client"
  assume_role_policy = data.aws_iam_policy_document.client_trust.json
}

resource "aws_iam_role_policy_attachment" "client_ssm" {
  role       = aws_iam_role.client.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "client" {
  name = "${var.project}-db-client"
  role = aws_iam_role.client.name
}

resource "aws_instance" "client" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.client_instance_type
  subnet_id                   = local.private_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.client.id]
  iam_instance_profile        = aws_iam_instance_profile.client.name
  associate_public_ip_address = false

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "${var.project}-db-client" }
}
