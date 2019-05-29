#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import numpy as np
import torch
import torch.optim
import torch.nn as nn

import os
import glob
import time
import sys
import shutil
import json
import pdb
from tqdm import tqdm
from tensorboardX import SummaryWriter

from Dataloader.Load_Data_new import get_loader, get_homography, \
                                     load_valid_set_file_all, write_lsq_results, load_image
from eval_lane import LaneEval
from Loss_crit import define_loss_crit, polynomial 
from Networks.LSQ_layer import Net
from Networks.utils import define_args, save_weightmap, first_run,\
                           mkdir_if_missing, Logger, define_init_weights,\
                           define_scheduler, define_optim, AverageMeter, save_weightmap_no_gt \



def main():
    global args
    global mse_policy
    parser = define_args()
    args = parser.parse_args()
    if not args.end_to_end:
        assert args.pretrained == False
    mse_policy = args.loss_policy == 'homography_mse'
    if args.clas:
        assert args.nclasses == 4

    # Check GPU availability
    if not args.no_cuda and not torch.cuda.is_available():
        raise Exception("No gpu available for usage")
    torch.backends.cudnn.benchmark = args.cudnn

    # Define save path
    save_id = 'Mod_{}_opt_{}_loss_{}_lr_{}_batch_{}_end2end_{}_lanes_{}_resize_{}_pretrain{}_clas{}' \
            .format(args.mod, args.optimizer,
                    args.loss_policy,
                    args.learning_rate,
                    args.batch_size,
                    args.end_to_end,
                    args.nclasses,
                    args.resize,
                    args.pretrained,
                    args.clas)

    # Compute file lsq parameters
    M_inv = get_homography(args.resize)

    # Dataloader for training and validation set
    input = load_image(args.image_dir, args.resize)

    # Define network
    model = Net(args)
    define_init_weights(model, args.weight_init)

    if not args.no_cuda:
        # Load model on gpu before passing params to optimizer
        model = model.cuda()

    # Define optimizer and scheduler
    optimizer = define_optim(args.optimizer, model.parameters(),
                             args.learning_rate, args.weight_decay)
    scheduler = define_scheduler(optimizer, args)

    # # Define loss criteria for multiple tasks
    # criterion, criterion_seg = define_loss_crit(args)
    # criterion_line_class = nn.CrossEntropyLoss().cuda()
    # criterion_horizon = nn.BCEWithLogitsLoss().cuda()

    # Name
    global crit_string
    crit_string = 'AREA**2' if args.end_to_end else 'ENTROPY'
    if args.clas:
        crit_string = 'TOT LOSS' 

    # Logging setup
    best_epoch = 0
    lowest_loss = np.inf
    log_file_name = 'log_train_start_0.txt'
    args.save_path = os.path.join(args.save_path, save_id)
    mkdir_if_missing(args.save_path)
    mkdir_if_missing(os.path.join(args.save_path, 'example/'))
    mkdir_if_missing(os.path.join(args.save_path, 'example/train'))
    mkdir_if_missing(os.path.join(args.save_path, 'example/valid'))

    # # Computes the file with lane data of the validation set
    # validation_set_path = os.path.join(args.save_path , 'validation_set.json')
    # load_valid_set_file_all(valid_idx, validation_set_path, args.image_dir) 
    # global valid_set_labels
    # global val_set_path
    # global ls_result_path
    # valid_set_labels = [json.loads(line) for line in open(validation_set_path).readlines()]
    # val_set_path = os.path.join(args.save_path, 'validation_set_dst.json')
    # ls_result_path = os.path.join(args.save_path, 'ls_result.json')

    # Tensorboard writer
    if not args.no_tb:
        global writer
        writer = SummaryWriter(os.path.join(args.save_path, 'Tensorboard/'))
    # Train, evaluate or resume
    args.resume = first_run(args.save_path)
    
    best_file_name = glob.glob(os.path.join(args.save_path, 'model_best*'))[0]
    if os.path.isfile(best_file_name):
        sys.stdout = Logger(os.path.join(args.save_path, 'Evaluate.txt'))
        print("=> loading checkpoint '{}'".format(best_file_name))
        checkpoint = torch.load(best_file_name)
        model.load_state_dict(checkpoint['state_dict'])
    else:
        print("=> no checkpoint found at '{}'".format(best_file_name))
    # validate(valid_loader, model, criterion, criterion_seg, 
    #         criterion_line_class, criterion_horizon, M_inv)
    test_image(model, input, M_inv)
    return

def test_image(model, input, M_inv):
    # Evaluate model
    model.eval()

    # Only forward pass, hence no gradients needed
    with torch.no_grad():
        i = 0
        # Start validation loop
        while True:
            i = i+1
            if not args.no_cuda:
                input = input.cuda(non_blocking=True)
                input = input.float()

            # Evaluate model
            try:
                beta0, beta1, beta2, beta3, weightmap_zeros, M, \
                output_net, outputs_line, outputs_horizon = model(input, args.end_to_end)
            except RuntimeError as e:
                print("Batch with idx {} skipped due to singular matrix".format(i))
                print(e)
                continue

            # Horizon task & Line classification task
            _, line_pred = torch.max(outputs_line, 1)

            #Write predictions to json file
            if args.clas:
                num_el = input.size(0)
                if args.nclasses > 2:
                    params_batch = torch.cat((beta0, beta1, beta2, beta3),2) \
                        .transpose(1, 2).data.tolist()
                else: 
                    params_batch = torch.cat((beta0, beta1),2).transpose(1, 2).data.tolist()
                    
                # line_type = line_pred.data.tolist()
                # horizon_pred = horizon_pred.data.tolist()
                # with open(val_set_path, 'w') as jsonFile:
                #     for j in range(num_el):
                #         im_id = index[j]
                #         json_line = valid_set_labels[im_id]
                #         line_id = line_type[j]
                #         horizon_est = horizon_pred[j]
                #         params = params_batch[j]
                #         json_line["params"] = params
                #         json_line["line_id"] = line_id
                #         json_line["horizon_est"] = horizon_est
                #         json.dump(json_line, jsonFile)
                #         jsonFile.write('\n')

            # Plot weightmap and curves                            
            save_weightmap_no_gt('valid', M, M_inv,
                            weightmap_zeros, beta0, beta1, beta2, beta3,
                            line_pred, 0, i, input, args.no_ortho, args.resize, args.save_path)
            print("iter %d \n"%(i))
        return

# def validate(loader, model, criterion, criterion_seg, 
#             criterion_line_class, criterion_horizon, M_inv, epoch=0):

#     # Define container to keep track of metric and loss
#     losses = AverageMeter()
#     avg_area = AverageMeter()
#     avg_trapezium_rule = AverageMeter()
#     acc_hor_tot = AverageMeter()
#     acc_line_tot = AverageMeter()

#     # Evaluate model
#     model.eval()

#     # Only forward pass, hence no gradients needed
#     with torch.no_grad():
        
#         # Start validation loop
#         for i, (input, gt, params, idx, gt_line, gt_horizon, index) in tqdm(enumerate(loader)):
#             if not args.no_cuda:
#                 # input, params = input.cuda(non_blocking=True), params.cuda(non_blocking=True)
#                 input = input.cuda(non_blocking=True)
#                 input = input.float()
#             # gt0, gt1, gt2, gt3 = params[:, 0, :], params[:, 1, :], params[:, 2, :], params[:, 3, :]

#             # Evaluate model
#             try:
#                 beta0, beta1, beta2, beta3, weightmap_zeros, M, \
#                 output_net, outputs_line, outputs_horizon = model(input, args.end_to_end)
#             except RuntimeError as e:
#                 print("Batch with idx {} skipped due to singular matrix".format(idx.numpy()))
#                 print(e)
#                 continue

#             # # Compute losses on parameters or segmentation
#             # if args.end_to_end:
#             #     loss = criterion(beta0, gt0) + criterion(beta1, gt1)
#             #     if args.nclasses > 3:
#             #         # Masks to create zero in the loss when lane line is not present
#             #         mask_llhs = torch.prod(gt2 != 0, 1) \
#             #                 .unsqueeze(1).unsqueeze(1).expand_as(beta2).type(torch.FloatTensor)
#             #         mask_rrhs = torch.prod(gt3 != 0, 1) \
#             #                 .unsqueeze(1).unsqueeze(1).expand_as(beta3).type(torch.FloatTensor)
#             #         if not args.no_cuda:
#             #             mask_llhs = mask_llhs.cuda()
#             #             mask_rrhs = mask_rrhs.cuda()
#             #         beta2 = beta2*mask_llhs
#             #         beta3 = beta3*mask_rrhs

#             #         # add losses of further lane lines
#             #         loss += criterion(beta2, gt2) + criterion(beta3, gt3)
#             # else:
#             #     gt = gt.cuda(non_blocking=True)
#             #     loss = criterion_seg(output_net, gt)
#             #     area = criterion(beta0, gt0) + criterion(beta1, gt1)
#             #     avg_area.update(area.item(), input.size(0))

#             # Horizon task & Line classification task
#             _, line_pred = torch.max(outputs_line, 1)
#             # if args.clas:
#             #     gt_horizon, gt_line = gt_horizon.cuda(non_blocking=True), \
#             #                           gt_line.cuda(non_blocking=True)
#             #     horizon_pred = torch.round(nn.Sigmoid()(outputs_horizon))
#             #     acc = torch.eq(horizon_pred, gt_horizon)
#             #     acc_hor = torch.sum(acc).float()/(args.resize*args.batch_size)
#             #     acc_hor_tot.update(acc_hor.item())
#             #     _, line_pred = torch.max(outputs_line, 1)
#             #     acc = torch.eq(line_pred, gt_line)
#             #     acc_line = torch.sum(acc).float()/(args.nclasses*args.batch_size)
#             #     acc_line_tot.update(acc_line.item())
#             #     loss_horizon = criterion_horizon(outputs_horizon, gt_horizon)
#             #     loss_line = criterion_line_class(outputs_line, gt_line)
#             #     loss = loss*args.weight_fit + (loss_line + loss_horizon)*args.weight_class
#             # else:
#             #     line_pred = gt_line

#             # # Exact area computation
#             # gt_left_lines = polynomial(gt0.cpu())
#             # gt_right_lines = polynomial(gt1.cpu())
#             # pred_left_lines = polynomial(beta0.cpu())
#             # pred_right_lines = polynomial(beta1.cpu())
#             # trap_left = pred_left_lines.trapezoidal(gt_left_lines)
#             # trap_right = pred_right_lines.trapezoidal(gt_right_lines)
#             # avg_trapezium_rule.update(((trap_left + trap_right)/2).mean().item(), input.size(0))
#             # losses.update(loss.item(), input.size(0))

#             #Write predictions to json file
#             if args.clas:
#                 num_el = input.size(0)
#                 if args.nclasses > 2:
#                     params_batch = torch.cat((beta0, beta1, beta2, beta3),2) \
#                         .transpose(1, 2).data.tolist()
#                 else: 
#                     params_batch = torch.cat((beta0, beta1),2).transpose(1, 2).data.tolist()
                    
#                 line_type = line_pred.data.tolist()
#                 horizon_pred = horizon_pred.data.tolist()
#                 with open(val_set_path, 'w') as jsonFile:
#                     for j in range(num_el):
#                         im_id = index[j]
#                         json_line = valid_set_labels[im_id]
#                         line_id = line_type[j]
#                         horizon_est = horizon_pred[j]
#                         params = params_batch[j]
#                         json_line["params"] = params
#                         json_line["line_id"] = line_id
#                         json_line["horizon_est"] = horizon_est
#                         json.dump(json_line, jsonFile)
#                         jsonFile.write('\n')

#             # # Print info
#             # if (i + 1) % args.print_freq == 0:
#             #         print('Test: [{0}/{1}]\t'
#             #               'Loss {loss.val:.8f} ({loss.avg:.8f})\t'
#             #               'Area {metric.val:.8f} ({metric.avg:.8f})'.format(
#             #                i+1, len(loader), loss=losses, metric=avg_area))

#             # # Plot weightmap and curves
#             # if (i + 1) % 25 == 0:
#             #     save_weightmap('valid', M, M_inv,
#             #                    weightmap_zeros, beta0, beta1, beta2, beta3,
#             #                    gt0, gt1, gt2, gt3, line_pred, gt, 0, i, input,
#             #                    args.no_ortho, args.resize, args.save_path)
#             # Plot weightmap and curves
#             save_weightmap('valid', f, M_inv,
#                             weightmap_zeros, beta0, beta1, beta2, beta3,
#                             line_pred, 0, i, input, args.no_ortho, args.resize, args.save_path)

#         # # Compute x, y coordinates for accuracy later
#         # if args.clas and args.nclasses > 3:
#         #     write_lsq_results(val_set_path, ls_result_path, args.nclasses, 
#         #             False, False, args.resize, no_ortho=args.no_ortho)
#         #     acc_seg = LaneEval.bench_one_submit(ls_result_path, val_set_path)
#         #     print("===> Average ACC_SEG on val is {:.8}".format(acc_seg[0]))

#         # if args.evaluate:
#         #     print("===> Average {}-loss on validation set is {:.8}".format(crit_string, 
#         #                                                                    losses.avg))
#         #     print("===> Average exact area on validation set is {:.8}".format(
#         #         avg_trapezium_rule.avg))
#         #     if not args.end_to_end:
#         #         print("===> Average area**2 on validation set is {:.8}".format(avg_area.avg))
#         #     if args.clas:
#         #         print("===> Average HORIZON ACC on val is {:.8}".format(acc_hor_tot.avg))
#         #         print("===> Average LINE ACC on val is {:.8}".format(acc_hor_tot.avg))

#         return losses.avg, avg_area.avg, avg_trapezium_rule.avg, acc_hor_tot.avg, acc_line_tot.avg


def save_checkpoint(state, to_copy, epoch):
    filepath = os.path.join(args.save_path, 'checkpoint_model_epoch_{}.pth.tar'.format(epoch))
    torch.save(state, filepath)
    if to_copy:
        if epoch > 0:
            lst = glob.glob(os.path.join(args.save_path, 'model_best*'))
            if len(lst) != 0:
                os.remove(lst[0])
        shutil.copyfile(filepath, os.path.join(args.save_path, 
            'model_best_epoch_{}.pth.tar'.format(epoch)))
        print("Best model copied")
    if epoch > 0:
        prev_checkpoint_filename = os.path.join(args.save_path, 
                'checkpoint_model_epoch_{}.pth.tar'.format(epoch-1))
        if os.path.exists(prev_checkpoint_filename):
            os.remove(prev_checkpoint_filename)


if __name__ == '__main__':
    main()
