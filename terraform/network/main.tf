data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Two AZs: the minimum for RDS subnet groups, EKS and an internet-facing ALB.
  # Check the plan: us-east-1e is not supported for EKS control planes.
  azs       = slice(data.aws_availability_zones.available.names, 0, 2)
  nat_count = var.nat_per_az ? length(local.azs) : 1
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # needed for EKS and RDS endpoints

  tags = { Name = var.project }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.project }
}

# --- Subnets ---------------------------------------------------------------

# Public: small /24s (10.0.0.0/24, 10.0.1.0/24). Hold the ALB and the NAT.
resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  map_public_ip_on_launch = true

  tags = {
    Name                     = "${var.project}-public-${local.azs[count.index]}"
    "kubernetes.io/role/elb" = "1" # AWS Load Balancer Controller: internet-facing ALBs
  }
}

# Private: /20s (10.0.16.0/20, 10.0.32.0/20). EKS pods take IPs from node subnets.
resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 1)

  tags = {
    Name                              = "${var.project}-private-${local.azs[count.index]}"
    "kubernetes.io/role/internal-elb" = "1" # internal load balancers
  }
}

# --- Public routing --------------------------------------------------------

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- NAT (the hourly cost in this layer) -----------------------------------

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = { Name = "${var.project}-nat-${count.index}" }
}

resource "aws_nat_gateway" "main" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id # NAT lives in a public subnet
  tags          = { Name = "${var.project}-${count.index}" }

  depends_on = [aws_internet_gateway.main]
}

# --- Private routing: one route table per AZ -------------------------------
# With nat_per_az = false every table points at NAT 0. Flipping the variable
# gives each AZ its own NAT with no other change.

resource "aws_route_table" "private" {
  count  = length(local.azs)
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-private-${local.azs[count.index]}" }
}

resource "aws_route" "private_nat" {
  count                  = length(local.azs)
  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[var.nat_per_az ? count.index : 0].id
}

resource "aws_route_table_association" "private" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# --- S3 gateway endpoint: free, keeps S3 (and ECR layer) traffic off the NAT

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = { Name = "${var.project}-s3" }
}
