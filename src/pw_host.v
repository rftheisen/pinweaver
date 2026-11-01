/*
 * Host command decoder: turns SPI bytes into memory / config / FIFO actions.
 * Commands are listed in docs/isa.md.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module pw_host (
    input  wire        clk,
    input  wire        rst_n,
    // from the SPI slave
    input  wire        active,
    input  wire        byte_start,
    input  wire        byte_valid,
    input  wire [7:0]  byte_data,
    input  wire        first_byte,
    output reg  [7:0]  resp,
    // instruction memory / config writes
    output reg         imem_we,
    output reg  [5:0]  imem_addr,
    output reg  [15:0] imem_data,
    output reg         cfg_we,
    output reg  [7:0]  cfg_addr,
    output reg  [7:0]  cfg_data,
    // engine control
    output reg  [1:0]  en,
    output reg  [1:0]  restart,
    output reg         la_arm,
    output reg         la_stop,
    // FIFOs
    output reg  [1:0]  tx_push,
    output reg  [7:0]  tx_data,
    output reg  [1:0]  rx_pop,
    input  wire [15:0] rx_head,  // {engine 1, engine 0}
    input  wire [1:0]  rx_empty,
    // debug read-back {la_trig, la_ptr, la_state, status, pc1, pc0}
    input  wire [47:0] dbg,
    // instruction memory read-back
    output wire [5:0]  rd_addr,
    input  wire [15:0] rd_data,
    // shared flags
    output reg  [3:0]  flag_set,
    output reg  [3:0]  flag_clr
);
  localparam C_IMEM = 8'h01, C_CFG = 8'h02, C_CTRL = 8'h03, C_TX = 8'h04,
             C_RX = 8'h05, C_DBG = 8'h06, C_FLAG = 8'h07, C_READ = 8'h08;

  reg [7:0] cmd;
  reg [3:0] phase;    // bytes received after the command byte (saturates)
  reg [7:0] addr;
  reg [7:0] hi;
  reg       hi_have;
  reg       sel;
  reg       pend;     // an RX byte was handed out; pop it when the next byte starts

  wire       rsel   = (phase == 4'd0) ? byte_data[0] : sel;
  wire [7:0] rx_sel = rsel ? rx_head[15:8] : rx_head[7:0];

  function [7:0] dbg_byte(input [3:0] i);
    case (i)
      4'd0: dbg_byte = dbg[7:0];
      4'd1: dbg_byte = dbg[15:8];
      4'd2: dbg_byte = dbg[23:16];
      4'd3: dbg_byte = dbg[31:24];
      4'd4: dbg_byte = dbg[39:32];
      4'd5: dbg_byte = dbg[47:40];
      default: dbg_byte = 8'd0;
    endcase
  endfunction

  // memory read-back: the address byte itself, then the auto-incrementing address
  assign rd_addr = (phase == 4'd0) ? byte_data[5:0] : addr[5:0];

  // the byte to send next, decided when a byte arrives
  always @(*) begin
    resp = 8'h00;
    if (first_byte) begin
      if (byte_data == C_DBG) resp = dbg_byte(4'd0);
    end else begin
      case (cmd)
        C_RX:  if (!rx_empty[rsel]) resp = rx_sel;
        C_DBG: resp = dbg_byte(phase + 4'd1);
        C_READ: resp = (phase != 4'd0 && hi_have) ? rd_data[7:0] : rd_data[15:8];
        default: ;
      endcase
    end
  end

  always @(posedge clk) begin
    imem_we <= 1'b0; cfg_we <= 1'b0; restart <= 2'd0; tx_push <= 2'd0; rx_pop <= 2'd0;
    la_arm <= 1'b0; la_stop <= 1'b0;
    flag_set <= 4'd0; flag_clr <= 4'd0;
    if (!rst_n) begin
      cmd <= 8'd0; phase <= 4'd0; addr <= 8'd0; hi <= 8'd0; hi_have <= 1'b0;
      sel <= 1'b0; pend <= 1'b0; en <= 2'd0;
      imem_addr <= 6'd0; imem_data <= 16'd0; cfg_addr <= 8'd0; cfg_data <= 8'd0; tx_data <= 8'd0;
    end else begin
      if (!active) pend <= 1'b0;
      if (byte_start && pend) begin
        rx_pop[sel] <= 1'b1;
        pend <= 1'b0;
      end
      if (byte_valid) begin
        if (first_byte) begin
          cmd <= byte_data; phase <= 4'd0; hi_have <= 1'b0;
        end else begin
          if (phase != 4'hF) phase <= phase + 4'd1;
          case (cmd)
            C_IMEM:
              if (phase == 4'd0) addr <= byte_data;
              else if (!hi_have) begin hi <= byte_data; hi_have <= 1'b1; end
              else begin
                imem_we <= 1'b1; imem_addr <= addr[5:0]; imem_data <= {hi, byte_data};
                addr <= addr + 8'd1; hi_have <= 1'b0;
              end
            C_CFG:
              if (phase == 4'd0) addr <= byte_data;
              else begin cfg_we <= 1'b1; cfg_addr <= addr; cfg_data <= byte_data; addr <= addr + 8'd1; end
            C_CTRL: begin
              en <= byte_data[1:0]; restart <= byte_data[5:4];
              la_arm <= byte_data[6]; la_stop <= byte_data[7];
            end
            C_READ:  // hi byte, then lo byte, then the next word
              if (phase == 4'd0) begin addr <= byte_data; hi_have <= 1'b1; end
              else if (hi_have) begin addr <= addr + 8'd1; hi_have <= 1'b0; end
              else hi_have <= 1'b1;
            C_TX:
              if (phase == 4'd0) sel <= byte_data[0];
              else begin tx_push[sel] <= 1'b1; tx_data <= byte_data; end
            C_RX: begin
              if (phase == 4'd0) sel <= byte_data[0];
              if (!rx_empty[rsel]) pend <= 1'b1;
            end
            C_FLAG: begin flag_set <= byte_data[3:0]; flag_clr <= byte_data[7:4]; end
            default: ;
          endcase
        end
      end
    end
  end
endmodule
