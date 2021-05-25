import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import argparse



from torch.optim import lr_scheduler
from torchvision import datasets, transforms
from torch.autograd import Variable

def squash(vec):
    lensq = vec.pow(2).sum(dim=2)
    length = lensq.sqrt()
    vec = vec * (lensq / (1 + lensq) / length).view(vec.size(0), vec.size(1), 1)
    return vec

class DynamicRouting(nn.Module):
    def __init__(self, input_caps, output_caps, n_iterations):
        super(DynamicRouting, self).__init__()
        self.n_iterations = n_iterations
        self.b = nn.Parameter(torch.zeros(input_caps, output_caps))

    def forward(self, u_hat):
        batch_size, input_caps, output_caps, output_dim = u_hat.size()

        c = F.softmax(self.b, dim=1)
        s = (c.unsqueeze(2) * u_hat).sum(dim=1)
        v = squash(s)

        if self.n_iterations > 0:
            b_batch = self.b.expand((batch_size, input_caps, output_caps))
            for r in range(self.n_iterations):
                v = v.unsqueeze(1)
                b_batch = b_batch + (u_hat * v).sum(-1)

                c = F.softmax(b_batch.view(-1, output_caps), dim=1).view(-1, input_caps, output_caps, 1)
                s = (c * u_hat).sum(dim=1)
                v = squash(s)

        return v


class FirstCapsuleLayer(nn.Module):
    def __init__(self, input_channels, output_caps, output_dim, kernel_size, stride):
        super(FirstCapsuleLayer, self).__init__()
        self.conv1 = nn.Conv2d(input_channels, output_caps * output_dim, kernel_size=kernel_size, stride=stride)
        self.input_channels = input_channels
        self.output_caps = output_caps
        self.output_dim = output_dim

    def forward(self, input):
        out = self.conv1(input)
        N, C, H, W = out.shape
        out = out.view(N, self.output_caps, self.output_dim, H, W)

        out = out.permute(0, 1, 3, 4, 2).contiguous()
        out = out.view(out.size(0), -1, out.size(4))

        out = squash(out)
        return out

class CapsLayer(nn.Module):
    def __init__(self, input_caps, input_dim, output_caps, output_dim, routing_module):
        super(CapsLayer, self).__init__()
        self.input_caps = input_caps
        self.input_dim = input_dim
        self.output_caps = output_caps
        self.output_dim = output_dim
        self.weights = nn.Parameter(torch.Tensor(input_caps, input_dim, output_dim * output_caps))
        self.routing_module = routing_module
        self.reset_parameters()
    
    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.input_caps)
        self.weights.data.uniform_(-stdv, stdv)
    
    def forward(self, x):
        x = x.unsqueeze(2)
        u_hat = x.matmul(self.weights)
        u_hat = u_hat.view(u_hat.size(0), self.input_caps, self.output_caps, self.output_dim)
        v = self.routing_module(u_hat)
        return v



class CapsuleNet(nn.Module):
    def __init__(
        self,
        routing_iterations,
        n_classes=10
    ):
        super(CapsuleNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 256, kernel_size=9, stride=1)
        self.firstCaps = FirstCapsuleLayer(256, 32, 8, kernel_size=9, stride=2)
        self.num_firstCaps = 32 * 6 * 6
        routing_module = DynamicRouting(self.num_firstCaps, n_classes, routing_iterations)
        self.Caps = CapsLayer(self.num_firstCaps, 8, n_classes, 16, routing_module)
    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.firstCaps(x)
        x = self.Caps(x)

        probs = x.pow(2).sum(dim=2).sqrt()
        return x, probs

class MarginLoss(nn.Module):
    def __init__(self, m_p, m_m, lambda_):
        super(MarginLoss, self).__init__()
        self.m_plus = m_p
        self.m_minus = m_m
        self.lambda_ = lambda_
    def forward(self, length, targets, loss_sum = True):
        t = torch.zeros(length.size()).long()
        if targets.is_cuda:
            t = t.cuda()
        t = t.scatter_(1, targets.data.view(-1, 1), 1)

        targets = Variable(t)
        losses = (targets.float() * F.relu(self.m_plus - length).pow(2) + 
                  self.lambda_ * (1. - targets.float()) * F.relu(length - self.m_minus).pow(2))
        return losses.mean() if loss_sum else losses.sum()


if __name__ == "__main__":


    # set command line arguments
    parser = argparse.ArgumentParser(description='CapsNet with MNIST')
    parser.add_argument('--batch-size', type=int, default=128, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=250, metavar='N',
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.001, metavar='LR',
                        help='learning rate (default: 0.01)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--routing_iterations', type=int, default=3)
    parser.add_argument('--with_reconstruction', action='store_true', default=False)
    args = parser.parse_args()
    args.cuda = not args.no_cuda and torch.cuda.is_available()


    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    kwargs = {'num_workers': 4, 'pin_memory': True} if args.cuda else {}

    train_loader = torch.utils.data.DataLoader(
        datasets.MNIST('../data', train=True, transform=transforms.Compose([
            transforms.ToTensor()
        ])),
        batch_size=args.batch_size, shuffle=True, **kwargs
    )

    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST('../data', train=False, transform=transforms.Compose([
            transforms.ToTensor()
        ])),
        batch_size=args.batch_size, shuffle=False, **kwargs
    )

    model = CapsuleNet(args.routing_iterations)

    # do reconstruction stuff here later
    if args.with_reconstruction:
        pass

    if args.cuda:
        model.cuda()
    
    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, verbose=True, patience=15, min_lr=1e-6)

    loss_fn = MarginLoss(0.9, 0.1, 0.5)

    def train(epoch):
        model.train()
        for batch_idx, (data, target) in enumerate(train_loader):
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            data, target = Variable(data), Variable(target, requires_grad=False)
            optimizer.zero_grad()
            # add reconstruction loss computation here
            output, probs = model(data)
            loss = loss_fn(probs, target)
            loss.backward()
            optimizer.step()
            if batch_idx % args.log_interval == 0:
                print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                    epoch, batch_idx * len(data), len(train_loader.dataset),
                           100. * batch_idx / len(train_loader), loss.item()))
    def test():
        model.eval()
        test_loss = 0
        correct = 0
        for data, target in test_loader:
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            data, target = Variable(data, volatile=True), Variable(target)
            # add reconstruction loss on test
            output, probs = model(data)
            test_loss += loss_fn(probs, target, size_average=False).data[0]

            pred = probs.data.max(1, keepdim=True)[1]  # get the index of the max probability
            correct += pred.eq(target.data.view_as(pred)).cpu().sum()

        test_loss /= len(test_loader.dataset)
        print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
            test_loss, correct, len(test_loader.dataset),
            100. * correct / len(test_loader.dataset)))
        return test_loss
    
    for epoch in range(1, args.epochs + 1):
        train(epoch)
        test_loss = test()
        scheduler.step(test_loss)
        torch.save(model.state_dict(),
                   '{:03d}_model_dict_{}routing.pth'.format(epoch, args.routing_iterations))
    

